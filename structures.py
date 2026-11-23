"""Construcción y evaluación determinista de estructuras de crédito.

Nada de esto llama a un LLM: primas, POP, EV, ROC y escenarios se calculan aquí
y se pueden testear. El LLM (opcional) solo redacta sobre el resultado.

Sobre el EV: con la probabilidad *risk-neutral* (=1−|Δ|) el valor esperado de
cualquier spread correctamente valorado es ≈0 por construcción, así que un
filtro "EV>0" con esa probabilidad rechazaría todo. Por eso se calculan dos
probabilidades:

  * `pop_rn`  = 1 − |Δ pata corta|  → la probabilidad que descuenta el mercado.
  * `pop`     = P(beneficio a vencimiento) bajo una **vol realizada** asumida
                (σ_rw = σ_ATM · realized_vol_factor, por defecto −10%).

El EV (`ev_pts`) integra el payoff completo sobre esa distribución lognormal con
deriva de forward (r−q), es decir: **solo se asume una hipótesis de volatilidad,
nunca una dirección del índice**. Con realized_vol_factor=1.0 el EV vuelve a ≈0.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from eurex_calendar import Contract, SPECS
from pricing import VolSurface, greeks, price, strike_for_delta

SIGN = {"sell": 1.0, "buy": -1.0}


# --------------------------------------------------------------------------- #
# Patas y estructuras
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    side: str          # "sell" | "buy"
    kind: str          # "put" | "call"
    strike: float
    iv: float
    price: float       # prima en puntos de índice
    delta: float
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0

    @property
    def sign(self) -> float:
        return SIGN[self.side]

    def intrinsic(self, spot_at_expiry: float) -> float:
        if self.kind == "call":
            return max(spot_at_expiry - self.strike, 0.0)
        return max(self.strike - spot_at_expiry, 0.0)

    def pnl_at(self, spot_at_expiry: float) -> float:
        """P&L en puntos de índice por contrato, a vencimiento."""
        return self.sign * (self.price - self.intrinsic(spot_at_expiry))

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "kind": self.kind,
            "strike": self.strike,
            "iv_pct": round(self.iv * 100, 2),
            "price": round(self.price, 2),
            "delta": round(self.delta, 4),
        }


@dataclass
class Spread:
    name: str
    kind: str                      # bull_put | bear_call | iron_condor
    index: str
    contract: Contract
    legs: list[Leg]
    spot: float
    atm_iv: float
    sigma_realized: float          # vol asumida para el EV
    dte_days: float
    expected_move: float
    multiplier: float
    credit: float                  # puntos
    width: float | None            # puntos
    max_profit: float              # puntos
    max_loss: float                # puntos
    breakevens: list[float]
    pop_rn: float                  # 1 - |Δ| de la pata corta (implícita)
    pop: float                     # P(beneficio) con vol realizada
    p_touch: float                 # aprox. P(tocar la pata corta antes de vencer)
    ev: float                      # puntos esperados por contrato (real-world)
    margin_estimate: float         # puntos por contrato
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def max_profit_eur(self) -> float:
        return self.max_profit * self.multiplier

    @property
    def max_loss_eur(self) -> float:
        return self.max_loss * self.multiplier

    @property
    def credit_eur(self) -> float:
        return self.credit * self.multiplier

    @property
    def roc(self) -> float:
        """Crédito / riesgo máximo del spread (no sobre margen PRMS de Eurex)."""
        return self.credit / self.max_loss if self.max_loss > 0 else 0.0

    @property
    def roc_margin(self) -> float:
        return self.credit / self.margin_estimate if self.margin_estimate > 0 else 0.0

    @property
    def accepted(self) -> bool:
        return not self.rejections

    @property
    def id(self) -> str:
        strikes = "/".join(f"{leg.strike:.0f}" for leg in self.legs)
        return f"{self.index}-{self.contract.ticker}-{self.kind}-{strikes}"

    def pnl_at(self, spot_at_expiry: float) -> float:
        return sum(leg.pnl_at(spot_at_expiry) for leg in self.legs)

    def scenarios(self, sigmas=(-2.0, -1.0, 0.0, 1.0, 2.0)) -> dict[str, dict]:
        out = {}
        for s in sigmas:
            spot_t = self.spot + s * self.expected_move
            pnl = self.pnl_at(spot_t)
            out[f"{s:+.0f}sigma"] = {
                "spot": round(spot_t, 1),
                "pnl_pts": round(pnl, 2),
                "pnl_eur": round(pnl * self.multiplier, 2),
            }
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "index": self.index,
            "contract": self.contract.ticker,
            "expiry": self.contract.expiry.isoformat(),
            "dte_days": round(self.dte_days, 2),
            "spot": round(self.spot, 1),
            "atm_iv_pct": round(self.atm_iv * 100, 2),
            "sigma_realized_pct": round(self.sigma_realized * 100, 2),
            "expected_move": round(self.expected_move, 1),
            "legs": [leg.to_dict() for leg in self.legs],
            "credit_pts": round(self.credit, 2),
            "credit_eur": round(self.credit_eur, 2),
            "width_pts": round(self.width, 1) if self.width is not None else None,
            "max_profit_eur": round(self.max_profit_eur, 2),
            "max_loss_eur": round(self.max_loss_eur, 2),
            "breakevens": [round(b, 1) for b in self.breakevens],
            "pop_rn": round(self.pop_rn, 4),
            "pop": round(self.pop, 4),
            "p_touch": round(self.p_touch, 4),
            "ev_pts": round(self.ev, 2),
            "ev_eur": round(self.ev * self.multiplier, 2),
            "roc": round(self.roc, 4),
            "roc_margin": round(self.roc_margin, 4),
            "margin_estimate_eur": round(self.margin_estimate * self.multiplier, 2),
            "score": round(self.score, 4),
            "scenarios": self.scenarios(),
            "warnings": self.warnings,
            "rejections": self.rejections,
            "accepted": self.accepted,
        }


# --------------------------------------------------------------------------- #
# Distribución a vencimiento y métricas reales
# --------------------------------------------------------------------------- #
def _distribution(spot: float, T: float, r: float, q: float, sigma: float, points: int = 401, z_max: float = 6.0):
    """Rejilla de precios a vencimiento y sus pesos (densidad lognormal en z)."""
    z = np.linspace(-z_max, z_max, points)
    weights = np.exp(-0.5 * z * z) / np.sqrt(2 * np.pi)
    weights = weights / weights.sum()          # normaliza la integral discreta
    spots = spot * np.exp((r - q - 0.5 * sigma * sigma) * T + sigma * np.sqrt(T) * z)
    return spots, weights


def market_metrics(
    legs: list[Leg],
    spot: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    points: int = 401,
) -> tuple[float, float]:
    """(EV en puntos, P(beneficio)) integrando el payoff con vol `sigma`."""
    if T <= 0:
        pnl = sum(leg.pnl_at(spot) for leg in legs)
        return float(pnl), float(pnl > 0)
    spots, weights = _distribution(spot, T, r, q, sigma, points=points)
    pnl = np.zeros_like(spots)
    for leg in legs:
        intrinsic = (
            np.maximum(spots - leg.strike, 0.0)
            if leg.kind == "call"
            else np.maximum(leg.strike - spots, 0.0)
        )
        pnl += leg.sign * (leg.price - intrinsic)
    ev = float((pnl * weights).sum())
    pop = float(((pnl > 0) * weights).sum())
    return ev, pop


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
@dataclass
class CandidateConfig:
    put_deltas: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    call_deltas: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    width_ticks: tuple[int, ...] = (2, 3, 4, 6, 8)
    realized_vol_factor: float = 0.90     # σ realizada asumida = σ implícita × este factor
    min_pop: float = 0.70
    min_roc: float = 0.10
    min_credit_pts: float = 1.0
    min_ev_pts: float = 0.0
    include_iron_condors: bool = True
    max_candidates: int = 12
    min_per_kind: int = 3


# --------------------------------------------------------------------------- #
# Construcción
# --------------------------------------------------------------------------- #
def _leg(spot, strike, T, surface, r, q, kind, side) -> Leg:
    iv = surface.iv(spot, strike, T, r, q)
    g = greeks(spot, strike, T, iv, r, q, kind)
    return Leg(
        side=side,
        kind=kind,
        strike=float(strike),
        iv=iv,
        price=price(spot, strike, T, iv, r, q, kind),
        delta=g["delta"],
        gamma=g["gamma"],
        theta=g["theta"],
        vega=g["vega"],
    )


def _finalize(spread: Spread, cfg: CandidateConfig) -> Spread:
    """Calcula score y filtros dejando constancia del motivo de cada rechazo."""
    if spread.max_loss <= 0:
        spread.rejections.append("max_loss no positivo")
    if spread.credit <= 0:
        spread.rejections.append("crédito no positivo")
    if spread.credit < cfg.min_credit_pts:
        spread.rejections.append(f"crédito {spread.credit:.1f} pts < mínimo {cfg.min_credit_pts}")
    if spread.pop < cfg.min_pop:
        spread.rejections.append(f"POP {spread.pop:.0%} < mínimo {cfg.min_pop:.0%}")
    if spread.roc < cfg.min_roc:
        spread.rejections.append(f"ROC {spread.roc:.1%} < mínimo {cfg.min_roc:.0%}")
    if spread.ev < cfg.min_ev_pts:
        spread.rejections.append(f"EV {spread.ev:.2f} pts < mínimo {cfg.min_ev_pts}")
    if spread.max_loss > 0:
        spread.score = spread.ev / spread.max_loss
    # avisos: informan, no bloquean
    if spread.pop > 0.92:
        spread.warnings.append("POP muy alto: el crédito es escaso para el riesgo asumido")
    if spread.width and spread.credit < 0.05 * spread.width:
        spread.warnings.append("crédito < 5% del ancho: la horquilla se come la operación")
    return spread


def _build(
    *,
    name: str,
    kind: str,
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    dte_days: float,
    expected_move: float,
    legs: list[Leg],
    width: float,
    breakevens: list[float],
    cfg: CandidateConfig,
) -> Spread:
    spec = SPECS[index]
    T = max(dte_days, 0.0) / 365.0
    credit = sum(leg.sign * leg.price for leg in legs)
    max_loss = max(width - credit, 0.0)
    sigma_rw = surface.atm * cfg.realized_vol_factor
    ev, pop = market_metrics(legs, spot, T, r, spec.q, sigma_rw)
    # suma de |delta| de las patas cortas: en un vertical es |Δ|; en un cóndor, |Δp|+|Δc|
    short_delta = sum(abs(leg.delta) for leg in legs if leg.side == "sell")
    spread = Spread(
        name=name,
        kind=kind,
        index=index,
        contract=contract,
        legs=legs,
        spot=spot,
        atm_iv=surface.atm,
        sigma_realized=sigma_rw,
        dte_days=dte_days,
        expected_move=expected_move,
        multiplier=spec.multiplier,
        credit=credit,
        width=width,
        max_profit=credit,
        max_loss=max_loss,
        breakevens=breakevens,
        pop_rn=float(np.clip(1.0 - short_delta, 0.0, 1.0)),
        pop=pop,
        p_touch=float(np.clip(2 * short_delta, 0.0, 0.99)),
        ev=ev,
        margin_estimate=max_loss,
    )
    spread.score = spread.ev / spread.max_loss if spread.max_loss > 0 else 0.0
    return _finalize(spread, cfg)


def bull_put_spread(
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    short_delta: float,
    width_ticks: int,
    cfg: CandidateConfig,
    dte_days: float,
    expected_move: float,
) -> Spread:
    spec = SPECS[index]
    tick = spec.strike_interval
    T = dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "put", short_delta, r, spec.q, tick)
    k_long = k_short - width_ticks * tick
    legs = [
        _leg(spot, k_short, T, surface, r, spec.q, "put", "sell"),
        _leg(spot, k_long, T, surface, r, spec.q, "put", "buy"),
    ]
    credit = legs[0].price - legs[1].price
    return _build(
        name=f"Bull Put {k_short:.0f}/{k_long:.0f}",
        kind="bull_put",
        index=index,
        contract=contract,
        spot=spot,
        surface=surface,
        r=r,
        dte_days=dte_days,
        expected_move=expected_move,
        legs=legs,
        width=k_short - k_long,
        breakevens=[k_short - credit],
        cfg=cfg,
    )


def bear_call_spread(
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    short_delta: float,
    width_ticks: int,
    cfg: CandidateConfig,
    dte_days: float,
    expected_move: float,
) -> Spread:
    spec = SPECS[index]
    tick = spec.strike_interval
    T = dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "call", short_delta, r, spec.q, tick)
    k_long = k_short + width_ticks * tick
    legs = [
        _leg(spot, k_short, T, surface, r, spec.q, "call", "sell"),
        _leg(spot, k_long, T, surface, r, spec.q, "call", "buy"),
    ]
    credit = legs[0].price - legs[1].price
    return _build(
        name=f"Bear Call {k_short:.0f}/{k_long:.0f}",
        kind="bear_call",
        index=index,
        contract=contract,
        spot=spot,
        surface=surface,
        r=r,
        dte_days=dte_days,
        expected_move=expected_move,
        legs=legs,
        width=k_long - k_short,
        breakevens=[k_short + credit],
        cfg=cfg,
    )


def iron_condor(
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    put_delta: float,
    call_delta: float,
    width_ticks: int,
    cfg: CandidateConfig,
    dte_days: float,
    expected_move: float,
) -> Spread:
    put = bull_put_spread(index, contract, spot, surface, r, put_delta, width_ticks, cfg, dte_days, expected_move)
    call = bear_call_spread(index, contract, spot, surface, r, call_delta, width_ticks, cfg, dte_days, expected_move)
    credit = put.credit + call.credit
    put_width = put.width or 0.0
    call_width = call.width or 0.0
    return _build(
        name=f"Iron Condor {put.legs[0].strike:.0f}/{call.legs[0].strike:.0f}",
        kind="iron_condor",
        index=index,
        contract=contract,
        spot=spot,
        surface=surface,
        r=r,
        dte_days=dte_days,
        expected_move=expected_move,
        legs=put.legs + call.legs,
        width=max(put_width, call_width),
        breakevens=[put.breakevens[0], call.breakevens[0]],
        cfg=cfg,
    )


def build_candidates(
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    cfg: CandidateConfig | None = None,
) -> list[Spread]:
    """Universo de estructuras: aceptadas y rechazadas (con el motivo del rechazo)."""
    cfg = cfg or CandidateConfig()
    dte_days = contract.days_to_expiry()
    T = max(dte_days, 0.0) / 365.0
    expected_move = spot * surface.atm * float(np.sqrt(T)) if T > 0 else 0.0
    out: list[Spread] = []
    for d in cfg.put_deltas:
        for w in cfg.width_ticks:
            out.append(bull_put_spread(index, contract, spot, surface, r, d, w, cfg, dte_days, expected_move))
    for d in cfg.call_deltas:
        for w in cfg.width_ticks:
            out.append(bear_call_spread(index, contract, spot, surface, r, d, w, cfg, dte_days, expected_move))
    if cfg.include_iron_condors:
        for dp in cfg.put_deltas:
            for dc in cfg.call_deltas:
                for w in cfg.width_ticks:
                    out.append(
                        iron_condor(index, contract, spot, surface, r, dp, dc, w, cfg, dte_days, expected_move)
                    )
    return out


def rank(candidates: list[Spread], cfg: CandidateConfig | None = None, include_rejected: bool = False) -> list[Spread]:
    """Ordena por score garantizando diversidad de estructuras.

    Ordenar solo por EV/riesgo deja fuera los verticales, porque un cóndor del
    mismo ancho cobra ~el doble de prima con el mismo max loss. Se reservan
    `min_per_kind` huecos por tipo antes de completar por score global.
    """
    cfg = cfg or CandidateConfig()
    accepted = sorted([c for c in candidates if c.accepted], key=lambda c: c.score, reverse=True)
    picked: list[Spread] = []
    seen: set[int] = set()
    for kind in ("bull_put", "bear_call", "iron_condor"):
        for spread in [c for c in accepted if c.kind == kind][: cfg.min_per_kind]:
            picked.append(spread)
            seen.add(id(spread))
    for spread in accepted:
        if len(picked) >= cfg.max_candidates:
            break
        if id(spread) not in seen:
            picked.append(spread)
            seen.add(id(spread))
    picked = picked[: cfg.max_candidates]
    picked.sort(key=lambda c: c.score, reverse=True)
    if include_rejected:
        rejected = sorted([c for c in candidates if not c.accepted], key=lambda c: c.score, reverse=True)
        return picked + rejected
    return picked
