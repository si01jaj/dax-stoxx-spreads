"""Tests de construcción de estructuras (structures.py) sin LLM ni red."""
from datetime import date

import pytest

from eurex_calendar import Contract
from pricing import VolSurface
from structures import (
    CandidateConfig,
    bear_call_spread,
    bull_put_spread,
    iron_condor,
)

SPOT = 24_000.0
R = 0.02
DTE = 30
EM = SPOT * 0.20 * (DTE / 365) ** 0.5     # expected move 1σ aproximado
CONTRATO = Contract(index="DAX", expiry=date(2026, 12, 18), kind="monthly")
SUPERFICIE = VolSurface(atm=0.20)

# Umbrales laxos: aquí se prueba la geometría de la estructura, no el filtro
CFG = CandidateConfig(min_pop=0.0, min_roc=-1.0, min_credit_pts=-1e9, min_ev_pts=-1e9)


def test_bull_put_spread_tiene_dos_puts_y_el_corto_arriba():
    spread = bull_put_spread("DAX", CONTRATO, SPOT, SUPERFICIE, R, 0.25, 4, CFG, DTE, EM)
    patas = spread.legs
    assert len(patas) == 2
    assert {p.kind for p in patas} == {"put"}
    corta = next(p for p in patas if p.side == "sell")
    larga = next(p for p in patas if p.side == "buy")
    assert corta.strike > larga.strike
    assert larga.strike < SPOT


def test_bear_call_spread_tiene_dos_calls_y_el_corto_abajo():
    spread = bear_call_spread("DAX", CONTRATO, SPOT, SUPERFICIE, R, 0.25, 4, CFG, DTE, EM)
    patas = spread.legs
    assert {p.kind for p in patas} == {"call"}
    corta = next(p for p in patas if p.side == "sell")
    larga = next(p for p in patas if p.side == "buy")
    assert corta.strike < larga.strike
    assert corta.strike > SPOT


def test_iron_condor_envuelve_el_spot():
    condor = iron_condor("DAX", CONTRATO, SPOT, SUPERFICIE, R, 0.20, 0.20, 4, CFG, DTE, EM)
    patas = condor.legs
    assert len(patas) == 4
    puts = [p.strike for p in patas if p.kind == "put"]
    calls = [p.strike for p in patas if p.kind == "call"]
    assert max(puts) < SPOT < min(calls)


def test_las_primas_son_positivas_y_las_deltas_coherentes():
    spread = bull_put_spread("DAX", CONTRATO, SPOT, SUPERFICIE, R, 0.25, 4, CFG, DTE, EM)
    for pata in spread.legs:
        assert pata.price > 0
        assert -1 < pata.delta < 0          # puts
    # el put más cercano al dinero (strike mayor) tiene la delta más negativa
    corta = next(p for p in spread.legs if p.side == "sell")
    larga = next(p for p in spread.legs if p.side == "buy")
    assert corta.strike > larga.strike
    assert corta.delta < larga.delta


@pytest.mark.parametrize("indice", ["DAX", "STOXX"])
def test_ambos_indices_construyen(indice):
    contrato = Contract(index=indice, expiry=date(2026, 12, 18), kind="monthly")
    spot = SPOT if indice == "DAX" else 5_600.0
    spread = bull_put_spread(indice, contrato, spot, SUPERFICIE, R, 0.20, 4, CFG, DTE, EM)
    assert len(spread.legs) == 2
    assert all(p.strike > 0 for p in spread.legs)
