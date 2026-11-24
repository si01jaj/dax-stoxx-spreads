"""Tests del calendario Eurex (eurex_calendar.py)."""
from datetime import date

import pytest

from eurex_calendar import (
    is_trading_day,
    monthly_expiry,
    previous_trading_day,
    third_friday,
    weekly_expiries,
)


def test_vencimiento_mensual_cae_en_viernes_habil():
    vencimiento = monthly_expiry(2026, 12)
    assert vencimiento.weekday() == 4      # viernes
    assert is_trading_day(vencimiento)
    assert vencimiento.day == 18           # tercer viernes de diciembre de 2026


def test_tercer_viernes():
    assert third_friday(2026, 12) == date(2026, 12, 18)
    assert third_friday(2027, 1) == date(2027, 1, 15)


def test_no_hay_semanal_en_la_semana_del_mensual():
    """En Eurex la semana del tercer viernes es el vencimiento mensual."""
    semanas = weekly_expiries(2026, 12)
    assert semanas
    assert 3 not in [numero for _, numero in semanas]
    assert all(0 < numero <= 5 for _, numero in semanas)


def test_fin_de_semana_no_es_habil():
    assert not is_trading_day(date(2026, 11, 28))   # sábado
    assert not is_trading_day(date(2026, 11, 29))   # domingo
    assert is_trading_day(date(2026, 11, 27))       # viernes


def test_previous_trading_day_retrocede_a_un_habil():
    previo = previous_trading_day(date(2026, 11, 23))   # lunes
    assert previo < date(2026, 11, 23)
    assert is_trading_day(previo)


@pytest.mark.parametrize("fecha", [date(2026, 11, 23), date(2026, 12, 18), date(2027, 3, 19)])
def test_los_vencimientos_calculados_son_operables(fecha):
    assert is_trading_day(fecha)
