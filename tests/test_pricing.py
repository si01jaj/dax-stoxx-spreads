"""Tests del motor de precios (pricing.py): Black-Scholes sobre forward."""
import math

import pytest

from pricing import VolSurface, greeks, implied_vol, price, round_to_tick, strike_grid

S, K, T, SIGMA, R, Q = 24_000.0, 24_000.0, 30 / 365, 0.20, 0.02, 0.0


def test_precio_atm_positivo():
    assert price(S, K, T, SIGMA, R, Q, "call") > 0
    assert price(S, K, T, SIGMA, R, Q, "put") > 0


def test_paridad_put_call():
    """C − P = DF · (F − K). Es la identidad que valida la implementación."""
    c = price(S, K, T, SIGMA, R, Q, "call")
    p = price(S, K, T, SIGMA, R, Q, "put")
    forward = S * math.exp((R - Q) * T)
    descuento = math.exp(-R * T)
    assert c - p == pytest.approx(descuento * (forward - K), abs=0.05)


def test_delta_call_y_put_en_su_rango():
    assert 0 < greeks(S, K, T, SIGMA, R, Q, "call")["delta"] < 1
    assert -1 < greeks(S, K, T, SIGMA, R, Q, "put")["delta"] < 0


def test_gamma_y_vega_positivos_theta_negativo():
    g = greeks(S, K, T, SIGMA, R, Q, "call")
    assert g["gamma"] > 0
    assert g["vega"] > 0
    assert g["theta"] < 0


def test_griegas_sin_plazo_son_cero():
    """A vencimiento no hay sensibilidad: ceros, no excepción."""
    g = greeks(S, K, 0, SIGMA, R, Q, "call")
    assert g == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


def test_griegas_rechazan_sigma_no_positiva():
    with pytest.raises(ValueError):
        greeks(S, K, T, 0, R, Q, "call")


def test_vol_implicita_ida_y_vuelta():
    prima = price(S, 23_000, T, 0.25, R, Q, "put")
    recuperada = implied_vol(prima, S, 23_000, T, R, Q, "put")
    assert recuperada == pytest.approx(0.25, abs=2e-3)


def test_vol_implicita_imposible_devuelve_none():
    assert implied_vol(-1.0, S, K, T, R, Q, "call") is None


def test_round_to_tick():
    assert round_to_tick(24_037, 50) == 24_050
    assert round_to_tick(24_024, 50) == 24_000


def test_strike_grid_cubre_el_entorno_y_respeta_el_tick():
    grid = strike_grid(S, tick=50, sigmas=2.0, sigma_pct=0.20)
    assert grid
    assert grid == sorted(grid)
    assert all(abs(k % 50) < 1e-9 for k in grid)
    assert min(grid) < S < max(grid)


def test_skew_encarece_los_puts_otm():
    surface = VolSurface(atm=0.20, skew=0.0217)
    # en el dinero la IV es la ATM salvo el pequeño efecto del forward
    atm = surface.iv(S, S, T, R, Q)
    assert atm == pytest.approx(0.20, abs=5e-3)
    put_otm = surface.iv(S, S * 0.85, T, R, Q)
    assert put_otm > atm
