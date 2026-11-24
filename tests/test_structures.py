"""Tests de construcción de estructuras: aritmética, POP, EV y dirección."""
from datetime import UTC, date, datetime

import pytest

from eurex_calendar import Contract
from pricing import VolSurface
from structures import (
    CandidateConfig,
    bear_call_spread,
    bear_put_spread,
    build_candidates,
    bull_call_spread,
    bull_put_spread,
    iron_condor,
    long_straddle,
    long_strangle,
    market_metrics,
    rank,
)

SPOT = 25000.0
R = 0.025
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)
CONTRACT = Contract("DAX", date(2026, 11, 27), "weekly", 4)          # ~4,04 días
DTE = CONTRACT.days_to_expiry(NOW)
T = DTE / 365.0
IV = 0.256
SURFACE = VolSurface(atm=IV, skew=0.0217)
CFG = CandidateConfig()


def _args(**kw):
    base = dict(index="DAX", contract=CONTRACT, spot=SPOT, surface=SURFACE, r=R,
                cfg=CFG, dte_days=DTE, expected_move=SPOT * IV * T ** 0.5)
    base.update(kw)
    return base


# ----------------------------- crédito ------------------------------------- #
def test_bull_put_aritmetica():
    s = bull_put_spread(short_delta=0.15, width_ticks=3, **_args())
    assert s.direction == "credit" and s.premium > 0
    assert s.width == 150.0
    assert s.max_profit == pytest.approx(s.premium)
    assert s.max_loss == pytest.approx(s.width - s.premium)
    assert s.roc == pytest.approx(s.premium / (s.width - s.premium))
    assert s.breakevens[0] == pytest.approx(s.legs[0].strike - s.premium)


def test_payoff_en_extremos_crédito():
    s = bull_put_spread(short_delta=0.15, width_ticks=3, **_args())
    k_short, k_long = s.legs[0].strike, s.legs[1].strike
    assert s.pnl_at(k_short + 500) == pytest.approx(s.premium)
    assert s.pnl_at(k_long - 500) == pytest.approx(-s.max_loss)
    assert s.pnl_at(s.breakevens[0]) == pytest.approx(0.0, abs=1e-9)


def test_pop_rn_es_complemento_del_delta():
    s = bull_put_spread(short_delta=0.15, width_ticks=3, **_args())
    assert s.pop_rn == pytest.approx(1 - abs(s.legs[0].delta), abs=1e-9)
    assert 0.80 < s.pop_rn < 0.92


def test_bear_call_aritmetica():
    s = bear_call_spread(short_delta=0.15, width_ticks=3, **_args())
    assert s.direction == "credit"
    assert s.legs[0].strike < s.legs[1].strike
    assert s.breakevens[0] == pytest.approx(s.legs[0].strike + s.premium)
    assert s.pnl_at(s.legs[0].strike - 500) == pytest.approx(s.premium)
    assert s.pnl_at(s.legs[1].strike + 500) == pytest.approx(-s.max_loss)


def test_iron_condor_coherente():
    s = iron_condor(put_delta=0.15, call_delta=0.15, width_ticks=3, **_args())
    assert len(s.legs) == 4
    assert s.breakevens[0] < SPOT < s.breakevens[1]
    assert s.max_loss > 0 and s.premium > 0
    assert s.pnl_at(SPOT) == pytest.approx(s.premium)


# ----------------------------- débito -------------------------------------- #
def test_bull_call_aritmetica():
    s = bull_call_spread(short_delta=0.35, width_ticks=4, **_args())
    assert s.direction == "debit"
    assert s.premium < 0
    assert s.debit == pytest.approx(-s.premium)
    assert s.max_loss == pytest.approx(s.debit)
    assert s.max_profit == pytest.approx(s.width - s.debit)
    assert s.legs[0].side == "buy" and s.legs[0].strike < s.legs[1].strike
    assert s.breakevens[0] == pytest.approx(s.legs[0].strike + s.debit)


def test_payoff_en_extremos_débito():
    s = bull_call_spread(short_delta=0.35, width_ticks=4, **_args())
    k_long, k_short = s.legs[0].strike, s.legs[1].strike
    assert s.pnl_at(k_long - 500) == pytest.approx(-s.max_loss)
    assert s.pnl_at(k_short + 500) == pytest.approx(s.max_profit)
    assert s.pnl_at(s.breakevens[0]) == pytest.approx(0.0, abs=1e-9)


def test_bear_put_aritmetica():
    s = bear_put_spread(short_delta=0.35, width_ticks=4, **_args())
    assert s.direction == "debit"
    assert s.legs[0].side == "buy" and s.legs[0].strike > s.legs[1].strike
    assert s.max_loss == pytest.approx(s.debit)
    assert s.max_profit == pytest.approx(s.width - s.debit)
    assert s.pnl_at(s.legs[1].strike - 500) == pytest.approx(s.max_profit)


def test_straddle():
    s = long_straddle(**_args())
    assert s.direction == "debit" and len(s.legs) == 2
    assert s.legs[0].strike == s.legs[1].strike
    assert abs(s.legs[0].strike - SPOT) <= 50
    assert s.max_profit is None and s.roc is None
    assert s.width is None
    assert s.max_loss == pytest.approx(s.debit)
    k = s.legs[0].strike
    assert s.breakevens[0] == pytest.approx(k - s.debit)
    assert s.breakevens[1] == pytest.approx(k + s.debit)
    assert s.pnl_at(k) == pytest.approx(-s.debit)
    assert s.pnl_at(k + 3 * s.debit) > 0


def test_strangle():
    s = long_strangle(delta=0.20, **_args())
    assert s.direction == "debit" and len(s.legs) == 2
    put, call = s.legs
    assert put.kind == "put" and put.strike < SPOT < call.strike
    assert s.max_profit is None and s.roc is None
    assert s.pnl_at(SPOT) == pytest.approx(-s.debit)
    assert s.pnl_at(call.strike + 3 * s.debit) > 0


# ----------------------------- dirección y EV ----------------------------- #
def test_ev_cero_sin_skew_y_sin_prima_de_vol():
    """Con superficie plana (sin skew) e IV = RV, el integrante devuelve EV≈0."""
    flat = VolSurface(atm=IV, skew=0.0)
    cfg = CandidateConfig(realized_vol_factor=1.0)
    for s in (
        bull_put_spread(short_delta=0.15, width_ticks=3, **_args(cfg=cfg, surface=flat, sigma_realized=IV)),
        bull_call_spread(short_delta=0.35, width_ticks=8, **_args(cfg=cfg, surface=flat, sigma_realized=IV)),
        long_straddle(**_args(cfg=cfg, surface=flat, sigma_realized=IV)),
    ):
        assert abs(s.ev) < 0.02 * s.max_loss, (s.kind, s.ev)


def test_compra_gana_si_la_vol_realizada_supera_la_implicita():
    """Con RV muy por encima de IV, comprar prima gana y vender pierde."""
    rv = IV * 1.40
    args = _args(sigma_realized=rv)
    venta = bull_put_spread(short_delta=0.15, width_ticks=3, **args)
    compras = [
        bull_call_spread(short_delta=0.35, width_ticks=8, **args),
        bear_put_spread(short_delta=0.35, width_ticks=8, **args),
        long_straddle(**args),
        long_strangle(delta=0.20, **args),
    ]
    assert venta.ev < 0
    assert max(c.ev for c in compras) > 0


def test_venta_gana_si_la_vol_realizada_es_menor():
    rv = IV * 0.85
    args = _args(sigma_realized=rv)
    assert bull_put_spread(short_delta=0.15, width_ticks=3, **args).ev > 0
    assert long_straddle(**args).ev < 0


def test_el_skew_tambien_es_valor_relativo():
    """Comprar el put cercano y vender el OTM (put debit) cobra el skew."""
    s = bear_put_spread(short_delta=0.35, width_ticks=6, **_args())
    assert s.direction == "debit"
    assert s.legs[0].iv < s.legs[1].iv      # compras IV menor, vendes IV mayor
    assert s.ev > 0                          # con RV = 0,9·IV ya hay ventaja


def test_distribucion_normalizada_y_ev_de_estructura_nula():
    ev, pop = market_metrics([], SPOT, T, R, 0.0, IV)
    assert ev == pytest.approx(0.0, abs=1e-12)
    assert pop == pytest.approx(0.0)


# ----------------------------- filtros y ranking -------------------------- #
def test_rechazos_dejan_motivo():
    cfg = CandidateConfig(min_pop=0.999, min_roc=5.0)
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg)
    assert rows and all(not r.accepted for r in rows)
    assert all(r.rejections for r in rows)
    assert rank(rows, cfg) == []


def test_determinista():
    a = bull_put_spread(short_delta=0.15, width_ticks=3, **_args()).to_dict()
    b = bull_put_spread(short_delta=0.15, width_ticks=3, **_args()).to_dict()
    assert a == b


def test_ranking_dos_por_clase():
    """2 mejores por clase: ninguna estructura queda enterrada por otra."""
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, CandidateConfig())
    top = rank(rows, CandidateConfig())
    assert len(top) <= CandidateConfig().max_candidates
    por_tipo: dict[str, int] = {}
    for r in top:
        por_tipo[r.kind] = por_tipo.get(r.kind, 0) + 1
    disponibles = {k: len([r for r in rows if r.accepted and r.kind == k]) for k in por_tipo}
    for kind, n in por_tipo.items():
        assert n == min(2, disponibles[kind]), kind
    assert [r.score for r in top] == sorted([r.score for r in top], reverse=True)


def test_ranking_rellena_si_faltan_clases():
    """Con dos clases disponibles devuelve 2 de cada: no infla el total."""
    cfg = CandidateConfig(include_iron_condors=False, include_debits=False, max_candidates=14)
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg)
    top = rank(rows, cfg)
    assert all(r.kind in ("bull_put", "bear_call") for r in top)
    assert len(top) == 2 * cfg.per_kind
    assert {r.kind for r in top} == {"bull_put", "bear_call"}


def test_aceptados_cumplen_sus_filtros():
    cfg = CandidateConfig()
    rows = [r for r in build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg) if r.accepted]
    assert rows, "con IV 25,6% y RV 23% debe haber candidatos"
    for r in rows:
        assert r.ev >= cfg.min_ev_pts
        if r.roc is not None:
            assert r.roc >= cfg.min_roc
        if r.is_credit:
            assert r.pop >= cfg.min_pop
            assert r.credit >= cfg.min_credit_pts
        else:
            assert r.debit <= cfg.max_debit_frac * r.spot


def test_anchos_escalan_con_el_plazo():
    """El ancho se dimensiona con el expected move: 4 DTE ≠ 81 DTE."""
    from structures import _width_ticks

    corto = _width_ticks(SPOT, SURFACE, 4 / 365, 50, CandidateConfig())
    largo = _width_ticks(SPOT, SURFACE, 81 / 365, 50, CandidateConfig())
    assert corto[0] >= 2                       # suelo en ticks
    assert max(corto) < max(largo)
    assert max(corto) * 50 < 0.5 * (SPOT * IV * (4 / 365) ** 0.5) * 3   # sigue siendo fracción del EM
    assert max(largo) * 50 <= 0.6 * (SPOT * IV * (81 / 365) ** 0.5)


def test_stoxx_usa_dividendo():
    from eurex_calendar import SPECS

    assert SPECS["STOXX"].q > 0 and SPECS["DAX"].q == 0
