"""Tests de construcción de estructuras: aritmética, POP y EV."""
from datetime import date, datetime, timezone

import pytest

from eurex_calendar import Contract
from pricing import VolSurface
from structures import (
    CandidateConfig,
    bear_call_spread,
    build_candidates,
    bull_put_spread,
    iron_condor,
    market_metrics,
    rank,
)

SPOT = 25000.0
R = 0.025
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
CONTRACT = Contract("DAX", date(2026, 11, 27), "weekly", 4)          # ~4,04 días
DTE = CONTRACT.days_to_expiry(NOW)
EM = SPOT * 0.20 * (DTE / 365) ** 0.5
SURFACE = VolSurface(atm=0.20, skew=0.0217)
CFG = CandidateConfig()


def _put(cfg: CandidateConfig = CFG, delta: float = 0.15, width: int = 3):
    return bull_put_spread("DAX", CONTRACT, SPOT, SURFACE, R, delta, width, cfg, DTE, EM)


def test_bull_put_aritmetica():
    s = _put()
    assert s.credit > 0
    assert s.width == 150.0
    assert s.max_profit == pytest.approx(s.credit)
    assert s.max_loss == pytest.approx(s.width - s.credit)
    assert s.max_profit_eur == pytest.approx(s.credit * 5)
    assert s.breakevens[0] == pytest.approx(s.legs[0].strike - s.credit)


def test_payoff_en_extremos():
    s = _put()
    k_short, k_long = s.legs[0].strike, s.legs[1].strike
    assert s.pnl_at(k_short + 500) == pytest.approx(s.credit)        # ambas OTM
    assert s.pnl_at(k_long - 500) == pytest.approx(-s.max_loss)      # ambas dentro
    assert s.pnl_at(k_short) == pytest.approx(s.credit)              # justo en el strike corto
    assert s.pnl_at(s.breakevens[0]) == pytest.approx(0.0, abs=1e-9)  # breakeven


def test_pop_rn_es_complemento_del_delta():
    s = _put(delta=0.15)
    assert s.pop_rn == pytest.approx(1 - abs(s.legs[0].delta), abs=1e-9)
    assert 0.80 < s.pop_rn < 0.92


def test_ev_es_cero_si_no_hay_prima_de_vol():
    """Con σ realizada = σ implícita el spread está bien valorado: EV ≈ 0."""
    cfg = CandidateConfig(realized_vol_factor=1.0)
    s = _put(cfg=cfg)
    assert abs(s.ev) < 0.02 * s.max_loss


def test_ev_positivo_con_vol_realizada_menor():
    """Vender a IV y realizar menos vol: EV positivo y POP mayor que la implícita."""
    s = _put(cfg=CandidateConfig(realized_vol_factor=0.85))
    assert s.ev > 0
    assert s.pop > s.pop_rn


def test_pop_real_igual_a_implicita_sin_prima_de_vol():
    s = _put(cfg=CandidateConfig(realized_vol_factor=1.0))
    assert s.pop == pytest.approx(s.pop_rn, abs=0.03)


def test_bear_call_aritmetica():
    s = bear_call_spread("DAX", CONTRACT, SPOT, SURFACE, R, 0.15, 3, CFG, DTE, EM)
    assert s.credit > 0
    assert s.legs[0].strike < s.legs[1].strike
    assert s.breakevens[0] == pytest.approx(s.legs[0].strike + s.credit)
    assert s.pnl_at(s.legs[0].strike - 500) == pytest.approx(s.credit)
    assert s.pnl_at(s.legs[1].strike + 500) == pytest.approx(-s.max_loss)


def test_iron_condor_coherente():
    s = iron_condor("DAX", CONTRACT, SPOT, SURFACE, R, 0.15, 0.15, 3, CFG, DTE, EM)
    assert len(s.legs) == 4
    assert s.breakevens[0] < SPOT < s.breakevens[1]
    assert s.max_loss > 0 and s.credit > 0
    assert s.pnl_at(SPOT) == pytest.approx(s.credit)
    assert s.pop_rn == pytest.approx(1 - 0.15 - 0.15, abs=0.03)


def test_distribucion_normalizada_y_ev_de_estructura_nula():
    """Sin patas el EV es 0 y la probabilidad 0: valida la integración."""
    ev, pop = market_metrics([], SPOT, DTE / 365, R, 0.0, 0.20)
    assert ev == pytest.approx(0.0, abs=1e-12)
    assert pop == pytest.approx(0.0)


def test_rechazos_dejan_motivo():
    cfg = CandidateConfig(min_pop=0.999, min_roc=5.0)
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg)
    assert rows and all(not r.accepted for r in rows)
    assert all(r.rejections for r in rows)
    assert rank(rows, cfg) == []


def test_determinista():
    assert _put().to_dict() == _put().to_dict()


def test_orden_por_score_y_limite():
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, CandidateConfig())
    top = rank(rows, CandidateConfig())
    assert 0 < len(top) <= CandidateConfig().max_candidates
    scores = [r.score for r in top]
    assert scores == sorted(scores, reverse=True)


def test_ranking_equilibrado_por_tipo():
    """4 por tipo: los verticales no quedan enterrados por los cóndores."""
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, CandidateConfig())
    top = rank(rows, CandidateConfig())
    kinds = [r.kind for r in top]
    for kind in ("bull_put", "bear_call", "iron_condor"):
        disponibles = [r for r in rows if r.accepted and r.kind == kind]
        assert kinds.count(kind) == min(len(disponibles), CandidateConfig().per_kind)
    assert len(top) == 12
    # y sigue ordenado por score global
    assert [r.score for r in top] == sorted([r.score for r in top], reverse=True)


def test_ranking_rellena_si_un_tipo_no_llega():
    """Si un tipo no tiene suficientes válidos, los huecos se rellenan con otros."""
    cfg = CandidateConfig(include_iron_condors=False, per_kind=4)
    rows = build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg)
    top = rank(rows, cfg)
    assert len(top) == CandidateConfig().max_candidates
    assert all(r.kind in ("bull_put", "bear_call") for r in top)


def test_aceptados_cumplen_todos_los_filtros():
    cfg = CandidateConfig()
    rows = [r for r in build_candidates("DAX", CONTRACT, SPOT, SURFACE, R, cfg) if r.accepted]
    assert rows, "con IV 20% y VRP 10% debe haber candidatos válidos"
    for r in rows:
        assert r.pop >= cfg.min_pop
        assert r.roc >= cfg.min_roc
        assert r.credit >= cfg.min_credit_pts
        assert r.ev >= cfg.min_ev_pts


def test_stoxx_usa_dividendo():
    """El STOXX 50 es índice de precios: q=3% debe afectar al precio."""
    from eurex_calendar import SPECS

    assert SPECS["STOXX"].q > 0 and SPECS["DAX"].q == 0
