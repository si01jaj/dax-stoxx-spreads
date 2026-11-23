"""Construcción y evaluación determinista de estructuras sobre ODAX / OESX.

Estructuras soportadas:
  crédito: bull put, bear call, iron condor
  débito : bull call, bear put, long straddle, long strangle

Nada de esto llama a un LLM: primas, POP, EV, ROC y escenarios se calculan aquí
y se pueden testear. El LLM (opcional) solo redacta sobre el resultado.

Sobre la dirección: quién gana (vender o comprar prima) lo decide el EV, y el EV
depende de una única hipótesis: la volatilidad que se espera realizar. Si la vol
realizada estimada es menor que la implícita, vender prima tiene EV positivo y
comprar lo tiene negativo; si es mayor, al revés. Nunca se asume dirección del
índice: la distribución a vencimiento usa deriva de forward (r−q).
"""
from __future__ import annotations

import os
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
    kind: str            # bull_put | bear_call | iron_condor | bull_call | bear_put | straddle | strangle
    direction: str       # "credit" | "debit"
    index: str
    contract: Contract
    legs: list[Leg]
    spot: float
    atm_iv: float
    sigma_realized: float          # vol asumida para el EV
    dte_days: float
    expected_move: float
    multiplier: float
    premium: float                 # prima neta en puntos: >0 cobras, <0 pagas
    width: float | None            # puntos (None en estructuras abiertas)
    max_profit: float | None       # puntos (None = ilimitado)
    max_loss: float                # puntos
    breakevens: list[float]
    pop_rn: float | None           # 1 - Σ|Δ cortas| (None si no hay patas vendidas)
    pop: float                     # P(beneficio) con vol realizada
    p_touch: float                 # aprox. P(tocar la pata corta antes de vencer)
    ev: float                      # puntos esperados por contrato (real-world)
    margin_estimate: float         # puntos por contrato
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def is_credit(self) -> bool:
        return self.direction == "credit"

    @property
    def credit(self) -> float:
        """Lo que cobras (0 en las de débito)."""
        return max(self.premium, 0.0)

    @property
    def debit(self) -> float:
        """Lo que pagas (0 en las de crédito)."""
        return max(-self.premium, 0.0)

    @property
    def max_profit_eur(self) -> float | None:
        return None if self.max_profit is None else self.max_profit * self.multiplier

    @property
    def max_loss_eur(self) -> float:
        return self.max_loss * self.multiplier

    @property
    def premium_eur(self) -> float:
        return self.premium * self.multiplier

    @property
    def roc(self) -> float | None:
        """Beneficio máximo / riesgo máximo. None si el beneficio es ilimitado."""
        if self.max_profit is None or self.max_loss <= 0:
            return None
        return self.max_profit / self.max_loss

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
            "direction": self.direction,
            "index": self.index,
            "contract": self.contract.ticker,
            "expiry": self.contract.expiry.isoformat(),
            "dte_days": round(self.dte_days, 2),
            "spot": round(self.spot, 1),
            "atm_iv_pct": round(self.atm_iv * 100, 2),
            "sigma_realized_pct": round(self.sigma_realized * 100, 2),
            "expected_move": round(self.expected_move, 1),
            "legs": [leg.to_dict() for leg in self.legs],
            "premium_pts": round(self.premium, 2),
            "premium_eur": round(self.premium_eur, 2),
            "width_pts": round(self.width, 1) if self.width is not None else None,
            "max_profit_eur": None if self.max_profit_eur is None else round(self.max_profit_eur, 2),
            "max_loss_eur": round(self.max_loss_eur, 2),
            "breakevens": [round(b, 1) for b in self.breakevens],
            "pop_rn": None if self.pop_rn is None else round(self.pop_rn, 4),
            "pop": round(self.pop, 4),
            "p_touch": round(self.p_touch, 4),
            "ev_pts": round(self.ev, 2),
            "ev_eur": round(self.ev * self.multiplier, 2),
            "roc": None if self.roc is None else round(self.roc, 4),
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
    weights = weights / weights.sum()
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
    return float((pnl * weights).sum()), float(((pnl > 0) * weights).sum())


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
@dataclass
class CandidateConfig:
    put_deltas: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    call_deltas: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    width_ticks: tuple[int, ...] = (2, 3, 4, 6, 8)
    debit_deltas: tuple[float, ...] = (0.25, 0.35, 0.45)
    strangle_deltas: tuple[float, ...] = (0.15, 0.20, 0.25)
    realized_vol_factor: float = float(os.getenv("REALIZED_VOL_FACTOR", "0.90"))
    min_pop: float = 0.70                 # solo para estructuras de crédito
    min_roc: float = 0.10                 # cuando el beneficio es finito
    min_credit_pts: float = 1.0
    min_ev_pts: float = 0.0
    max_debit_frac: float = 0.02          # débito máximo como fracción del spot
    include_iron_condors: bool = True
    include_debits: bool = True
    include_vol_structures: bool = True   # straddle y strangle
    max_candidates: int = 14
    per_kind: int = 2                     # las 2 mejores de cada clase


CREDIT_KINDS = ("bull_put", "bear_call", "iron_condor")
DEBIT_KINDS = ("bull_call", "bear_put", "straddle", "strangle")
ALL_KINDS = CREDIT_KINDS + DEBIT_KINDS


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
    if spread.premium == 0:
        spread.rejections.append("prima nula")
    if spread.is_credit:
        if spread.credit < cfg.min_credit_pts:
            spread.rejections.append(f"crédito {spread.credit:.1f} pts < mínimo {cfg.min_credit_pts}")
        if spread.pop < cfg.min_pop:
            spread.rejections.append(f"POP {spread.pop:.0%} < mínimo {cfg.min_pop:.0%}")
    else:
        limite = cfg.max_debit_frac * spread.spot
        if spread.debit > limite:
            spread.rejections.append(f"débito {spread.debit:.0f} pts > máximo {limite:.0f}")
    if spread.roc is not None and spread.roc < cfg.min_roc:
        spread.rejections.append(f"ROC {spread.roc:.1%} < mínimo {cfg.min_roc:.0%}")
    if spread.ev < cfg.min_ev_pts:
        spread.rejections.append(f"EV {spread.ev:.2f} pts < mínimo {cfg.min_ev_pts}")
    spread.score = spread.ev / spread.max_loss if spread.max_loss > 0 else 0.0
    # avisos: informan, no bloquean
    if spread.is_credit and spread.pop > 0.92:
        spread.warnings.append("POP muy alto: el crédito es escaso para el riesgo asumido")
    if spread.width and spread.is_credit and spread.credit < 0.05 * spread.width:
        spread.warnings.append("crédito < 5% del ancho: la horquilla se come la operación")
    if spread.max_profit is None:
        spread.warnings.append("beneficio ilimitado: ROC no comparable con verticales")
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
    width: float | None,
    breakevens: list[float],
    cfg: CandidateConfig,
    sigma_realized: float | None = None,
) -> Spread:
    spec = SPECS[index]
    T = max(dte_days, 0.0) / 365.0
    premium = sum(leg.sign * leg.price for leg in legs)
    direction = "credit" if premium >= 0 else "debit"
    sigma_rw = sigma_realized or surface.atm * cfg.realized_vol_factor
    ev, pop = market_metrics(legs, spot, T, r, spec.q, sigma_rw)

    if direction == "credit":
        max_profit: float | None = premium
        max_loss = max((width or 0.0) - premium, 0.0)
    else:
        max_loss = -premium
        max_profit = None if width is None else max(width - max_loss, 0.0)

    short_delta = sum(abs(leg.delta) for leg in legs if leg.side == "sell")
    spread = Spread(
        name=name,
        kind=kind,
        direction=direction,
        index=index,
        contract=contract,
        legs=legs,
        spot=spot,
        atm_iv=surface.atm,
        sigma_realized=sigma_rw,
        dte_days=dte_days,
        expected_move=expected_move,
        multiplier=spec.multiplier,
        premium=premium,
        width=width,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
        pop_rn=float(np.clip(1.0 - short_delta, 0.0, 1.0)) if short_delta > 0 else None,
        pop=pop,
        p_touch=float(np.clip(2 * short_delta, 0.0, 0.99)),
        ev=ev,
        margin_estimate=max_loss,
    )
    spread.score = spread.ev / spread.max_loss if spread.max_loss > 0 else 0.0
    return _finalize(spread, cfg)


# ---------------------------- estructuras de crédito ----------------------- #
def bull_put_spread(index, contract, spot, surface, r, short_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized=None):
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "put", short_delta, r, spec.q, tick)
    k_long = k_short - width_ticks * tick
    legs = [
        _leg(spot, k_short, T, surface, r, spec.q, "put", "sell"),
        _leg(spot, k_long, T, surface, r, spec.q, "put", "buy"),
    ]
    credit = legs[0].price - legs[1].price
    return _build(
        name=f"Bull Put {k_short:.0f}/{k_long:.0f}", kind="bull_put", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=k_short - k_long,
        breakevens=[k_short - credit], cfg=cfg, sigma_realized=sigma_realized,
    )


def bear_call_spread(index, contract, spot, surface, r, short_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized=None):
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "call", short_delta, r, spec.q, tick)
    k_long = k_short + width_ticks * tick
    legs = [
        _leg(spot, k_short, T, surface, r, spec.q, "call", "sell"),
        _leg(spot, k_long, T, surface, r, spec.q, "call", "buy"),
    ]
    credit = legs[0].price - legs[1].price
    return _build(
        name=f"Bear Call {k_short:.0f}/{k_long:.0f}", kind="bear_call", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=k_long - k_short,
        breakevens=[k_short + credit], cfg=cfg, sigma_realized=sigma_realized,
    )


def iron_condor(index, contract, spot, surface, r, put_delta, call_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized=None):
    put = bull_put_spread(index, contract, spot, surface, r, put_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized)
    call = bear_call_spread(index, contract, spot, surface, r, call_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized)
    put_width = put.width or 0.0
    call_width = call.width or 0.0
    return _build(
        name=f"Iron Condor {put.legs[0].strike:.0f}/{call.legs[0].strike:.0f}", kind="iron_condor",
        index=index, contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=put.legs + call.legs, width=max(put_width, call_width),
        breakevens=[put.breakevens[0], call.breakevens[0]], cfg=cfg, sigma_realized=sigma_realized,
    )


# ---------------------------- estructuras de débito ------------------------ #
def bull_call_spread(index, contract, spot, surface, r, short_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized=None):
    """Débito: compra el call bajo y vende el call alto."""
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "call", short_delta, r, spec.q, tick)
    k_long = k_short - width_ticks * tick
    legs = [
        _leg(spot, k_long, T, surface, r, spec.q, "call", "buy"),
        _leg(spot, k_short, T, surface, r, spec.q, "call", "sell"),
    ]
    debit = legs[0].price - legs[1].price
    return _build(
        name=f"Bull Call {k_long:.0f}/{k_short:.0f}", kind="bull_call", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=k_short - k_long,
        breakevens=[k_long + debit], cfg=cfg, sigma_realized=sigma_realized,
    )


def bear_put_spread(index, contract, spot, surface, r, short_delta, width_ticks, cfg, dte_days, expected_move, sigma_realized=None):
    """Débito: compra el put alto y vende el put bajo."""
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k_short = strike_for_delta(spot, T, surface, "put", short_delta, r, spec.q, tick)
    k_long = k_short + width_ticks * tick
    legs = [
        _leg(spot, k_long, T, surface, r, spec.q, "put", "buy"),
        _leg(spot, k_short, T, surface, r, spec.q, "put", "sell"),
    ]
    debit = legs[0].price - legs[1].price
    return _build(
        name=f"Bear Put {k_long:.0f}/{k_short:.0f}", kind="bear_put", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=k_long - k_short,
        breakevens=[k_long - debit], cfg=cfg, sigma_realized=sigma_realized,
    )


def long_straddle(index, contract, spot, surface, r, cfg, dte_days, expected_move, sigma_realized=None):
    """Débito: compra call y put en el strike más cercano al dinero."""
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k = round(spot / tick) * tick
    legs = [
        _leg(spot, k, T, surface, r, spec.q, "call", "buy"),
        _leg(spot, k, T, surface, r, spec.q, "put", "buy"),
    ]
    debit = legs[0].price + legs[1].price
    return _build(
        name=f"Straddle {k:.0f}", kind="straddle", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=None,
        breakevens=[k - debit, k + debit], cfg=cfg, sigma_realized=sigma_realized,
    )


def long_strangle(index, contract, spot, surface, r, delta, cfg, dte_days, expected_move, sigma_realized=None):
    """Débito: compra put y call OTM a la misma distancia en delta."""
    spec = SPECS[index]
    tick, T = spec.strike_interval, dte_days / 365.0
    k_put = strike_for_delta(spot, T, surface, "put", delta, r, spec.q, tick)
    k_call = strike_for_delta(spot, T, surface, "call", delta, r, spec.q, tick)
    legs = [
        _leg(spot, k_put, T, surface, r, spec.q, "put", "buy"),
        _leg(spot, k_call, T, surface, r, spec.q, "call", "buy"),
    ]
    debit = legs[0].price + legs[1].price
    return _build(
        name=f"Strangle {k_put:.0f}/{k_call:.0f}", kind="strangle", index=index,
        contract=contract, spot=spot, surface=surface, r=r, dte_days=dte_days,
        expected_move=expected_move, legs=legs, width=None,
        breakevens=[k_put - debit, k_call + debit], cfg=cfg, sigma_realized=sigma_realized,
    )


# --------------------------------------------------------------------------- #
# Universo y ranking
# --------------------------------------------------------------------------- #
def build_candidates(
    index: str,
    contract: Contract,
    spot: float,
    surface: VolSurface,
    r: float,
    cfg: CandidateConfig | None = None,
    sigma_realized: float | None = None,
) -> list[Spread]:
    """Universo de estructuras: aceptadas y rechazadas (con el motivo del rechazo)."""
    cfg = cfg or CandidateConfig()
    dte_days = contract.days_to_expiry()
    T = max(dte_days, 0.0) / 365.0
    em = spot * surface.atm * float(np.sqrt(T)) if T > 0 else 0.0
    kw = dict(index=index, contract=contract, spot=spot, surface=surface, r=r,
              cfg=cfg, dte_days=dte_days, expected_move=em, sigma_realized=sigma_realized)
    out: list[Spread] = []
    for d in cfg.put_deltas:
        for w in cfg.width_ticks:
            out.append(bull_put_spread(short_delta=d, width_ticks=w, **kw))
    for d in cfg.call_deltas:
        for w in cfg.width_ticks:
            out.append(bear_call_spread(short_delta=d, width_ticks=w, **kw))
    if cfg.include_iron_condors:
        for dp in cfg.put_deltas:
            for dc in cfg.call_deltas:
                for w in cfg.width_ticks:
                    out.append(iron_condor(put_delta=dp, call_delta=dc, width_ticks=w, **kw))
    if cfg.include_debits:
        for d in cfg.debit_deltas:
            for w in cfg.width_ticks:
                out.append(bull_call_spread(short_delta=d, width_ticks=w, **kw))
                out.append(bear_put_spread(short_delta=d, width_ticks=w, **kw))
        if cfg.include_vol_structures:
            out.append(long_straddle(**kw))
            for d in cfg.strangle_deltas:
                out.append(long_strangle(delta=d, **kw))
    return out


def rank(candidates: list[Spread], cfg: CandidateConfig | None = None, include_rejected: bool = False) -> list[Spread]:
    """Las `per_kind` mejores de cada clase, ordenadas por score (EV / riesgo).

    Ordenar por EV/riesgo sin cuota por tipo entierra clases enteras: un iron
    condor del mismo ancho cobra ~el doble de prima con el mismo max loss y
    siempre gana el cociente. Se reservan `per_kind` huecos por clase y los
    sobrantes se rellenan por score global.
    """
    cfg = cfg or CandidateConfig()
    accepted = sorted([c for c in candidates if c.accepted], key=lambda c: c.score, reverse=True)
    kinds_present = {c.kind for c in accepted}
    target = min(cfg.max_candidates, cfg.per_kind * len(kinds_present))
    picked: list[Spread] = []
    seen: set[int] = set()
    for kind in ALL_KINDS:
        for spread in [c for c in accepted if c.kind == kind][: cfg.per_kind]:
            picked.append(spread)
            seen.add(id(spread))
    if len(picked) < target:          # solo si alguna clase no tenía `per_kind` válidas
        for spread in accepted:
            if len(picked) >= target:
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
