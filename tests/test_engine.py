"""Tests de la lógica pura del motor: vencimientos, tickers EUREX, griegas y rejilla.

Nada de red: si estos tests fallan, el fallo es de cálculo, no de Yahoo.
"""
from datetime import date

import pytest

import main as engine


# ── Vencimientos ────────────────────────────────────────────────────────
def test_friday_week_of_month_mensual():
    # diciembre 2026: viernes 4, 11, 18, 25 -> el 18 es el tercero
    assert engine.friday_week_of_month(date(2026, 12, 18)) == 3


@pytest.mark.parametrize("day,expected", [(4, 1), (11, 2), (18, 3), (25, 4)])
def test_friday_week_of_month_todas(day, expected):
    assert engine.friday_week_of_month(date(2026, 12, day)) == expected


def test_contract_ticker_mensual_y_semanales():
    assert engine.contract_ticker("DAX 40", date(2026, 12, 18)) == "ODAX"
    assert engine.contract_ticker("DAX 40", date(2026, 12, 11)) == "ODAX2"
    assert engine.contract_ticker("EURO STOXX 50", date(2026, 12, 11)) == "OESX2"
    assert engine.contract_ticker("EURO STOXX 50", date(2026, 12, 18)) == "OESX"


@pytest.mark.parametrize("kind", ["semanal", "mensual", "2meses", "3meses"])
def test_get_expiry_devuelve_viernes_futuro(kind):
    expiry, dte = engine.get_expiry(kind)
    assert expiry.weekday() == 4, "el vencimiento debe caer en viernes"
    assert dte > 0, "no se puede operar un vencimiento con 0 días"
    assert dte < 120, "un horizonte de más de 4 meses indica un cálculo roto"


def test_get_expiry_semanal_evita_el_mensual():
    expiry, _ = engine.get_expiry("semanal")
    assert engine.friday_week_of_month(expiry) != 3


# ── Griegas ─────────────────────────────────────────────────────────────
def test_delta_put_call_parity_atm():
    g = engine.calculate_greeks(S=24000, K=24000, T=30 / 365, sigma=0.20)
    # ATM: delta call - delta put debe ser ~1 (con q=0)
    assert abs((g["call_delta"] - g["put_delta"]) - 1.0) < 0.01
    assert 0.45 < g["call_delta"] < 0.60


def test_griegas_positivas_y_signos():
    g = engine.calculate_greeks(S=24000, K=24000, T=30 / 365, sigma=0.20)
    assert g["gamma"] > 0
    assert g["vega"] > 0
    assert g["theta_call"] < 0, "una call ATM pierde valor con el paso del tiempo"
    assert g["put_delta"] < 0


def test_griegas_decaimiento_por_dte():
    corto = engine.calculate_greeks(S=24000, K=24000, T=7 / 365, sigma=0.20)
    largo = engine.calculate_greeks(S=24000, K=24000, T=60 / 365, sigma=0.20)
    assert corto["gamma"] > largo["gamma"], "gamma crece al acercarse el vencimiento"
    assert corto["vega"] < largo["vega"], "vega cae al acercarse el vencimiento"


@pytest.mark.parametrize("T,sigma", [(0, 0.2), (-1, 0.2), (30 / 365, 0)])
def test_griegas_entradas_invalidas(T, sigma):
    assert engine.calculate_greeks(S=24000, K=24000, T=T, sigma=sigma) is None


# ── Rejilla de strikes ──────────────────────────────────────────────────
@pytest.mark.parametrize("price,interval", [(24000, 50), (5600, 25), (24137, 50)])
def test_strikes_son_multiplos_y_ordenados(price, interval):
    strikes = engine.generate_strikes(price, interval, 0.06)
    assert strikes == sorted(strikes)
    assert all(s % interval == 0 for s in strikes)
    assert len(strikes) == len(set(strikes)), "sin strikes duplicados"


def test_strikes_cubren_el_rango():
    price, interval, rng = 24000, 50, 0.06
    strikes = engine.generate_strikes(price, interval, rng)
    assert min(strikes) <= price * (1 - rng) + interval
    assert max(strikes) >= price * (1 + rng) - interval
    # el strike más cercano al precio no debe alejarse más de medio intervalo
    assert min(abs(s - price) for s in strikes) <= interval / 2


# ── Cadena completa (sin red) ───────────────────────────────────────────
def test_build_chain_estructura_y_expected_move():
    cfg = engine.INDICES["DAX 40"]
    expiry, dte = engine.get_expiry("semanal")
    chain = engine.build_chain("DAX 40", 24000.0, 0.20, dte, cfg, expiry)

    assert chain["multiplier"] == 5
    assert chain["contract"].startswith("ODAX")
    assert chain["options"], "la rejilla no puede salir vacía"
    expected = 24000 * 0.20 * (max(dte, 1) / 365) ** 0.5
    assert abs(chain["expected_move_1sd"] - expected) < 1.0
    assert chain["rally_scenario"] > chain["price"] > chain["drop_scenario"]
