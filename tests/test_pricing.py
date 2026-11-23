"""Tests de precios, griegas y superficie de volatilidad."""
import numpy as np
import pytest

from pricing import (
    VolSurface,
    forward,
    greeks,
    implied_vol,
    price,
    round_to_tick,
    strike_for_delta,
)

SPOT = 25000.0
R = 0.025
Q = 0.0
T = 7 / 365.0
SIGMA = 0.20


def test_paridad_put_call():
    K = 24800.0
    c = price(SPOT, K, T, SIGMA, R, Q, "call")
    p = price(SPOT, K, T, SIGMA, R, Q, "put")
    F = forward(SPOT, T, R, Q)
    DF = np.exp(-R * T)
    assert c - p == pytest.approx(DF * (F - K), abs=1e-8)


def test_intrinseco_a_vencimiento():
    assert price(SPOT, 24000, 0.0, SIGMA, R, Q, "put") == 0.0
    assert price(SPOT, 26000, 0.0, SIGMA, R, Q, "call") == 0.0
    assert price(SPOT, 26000, 0.0, SIGMA, R, Q, "put") == pytest.approx(1000.0)
    assert price(SPOT, 24000, 0.0, SIGMA, R, Q, "call") == pytest.approx(1000.0)


def test_griegas_basicas():
    g = greeks(SPOT, SPOT, T, SIGMA, R, Q, "call")
    assert 0.45 < g["delta"] < 0.62
    assert g["gamma"] > 0 and g["vega"] > 0
    assert g["theta"] < 0
    gput = greeks(SPOT, SPOT, T, SIGMA, R, Q, "put")
    assert -0.55 < gput["delta"] < -0.38
    # theta del put es menos negativo que el del call con r>0
    assert gput["theta"] > g["theta"]


def test_delta_monotonia():
    deltas = [abs(greeks(SPOT, k, T, SIGMA, R, Q, "put")["delta"]) for k in (23000, 24000, 25000)]
    assert deltas[0] < deltas[1] < deltas[2]


def test_vol_implicita_ida_y_vuelta():
    for K in (23000, 24500, 25000, 25800):
        for kind in ("call", "put"):
            p = price(SPOT, K, T, SIGMA, R, Q, kind)
            iv = implied_vol(p, SPOT, K, T, R, Q, kind)
            assert iv == pytest.approx(SIGMA, abs=2e-3)


def test_superficie_skew_en_puts_y_calls():
    s = VolSurface(atm=0.20, skew=0.0217)
    iv_put = s.iv(SPOT, 23000, T, R, Q)
    iv_atm = s.iv(SPOT, SPOT, T, R, Q)
    iv_call = s.iv(SPOT, 27000, T, R, Q)
    assert iv_put > iv_atm > iv_call
    assert 0.20 < iv_atm < 0.21


def test_superficie_respeta_limites():
    s = VolSurface(atm=0.20, skew=0.5)
    assert 0.02 <= s.iv(SPOT, 15000, T, R, Q) <= 3.0


def test_strike_para_delta_se_redondea_al_tick():
    s = VolSurface(atm=0.20, skew=0.0217)
    k10 = strike_for_delta(SPOT, T, s, "put", 0.10, R, Q, tick=50)
    assert k10 % 50 == 0
    d = abs(greeks(SPOT, k10, T, s.iv(SPOT, k10, T, R, Q), R, Q, "put")["delta"])
    assert d == pytest.approx(0.10, abs=0.02)

    kc = strike_for_delta(SPOT, T, s, "call", 0.15, R, Q, tick=50)
    dc = greeks(SPOT, kc, T, s.iv(SPOT, kc, T, R, Q), R, Q, "call")["delta"]
    assert dc == pytest.approx(0.15, abs=0.02)
    assert kc > SPOT > k10


def test_round_to_tick():
    assert round_to_tick(24749.0, 50) == 24750.0
    assert round_to_tick(24724.0, 50) == 24700.0


def test_dividendo_desplaza_el_forward():
    f_no_div = forward(6000, 81 / 365, 0.025, 0.0)
    f_div = forward(6000, 81 / 365, 0.025, 0.03)
    assert f_div < f_no_div
