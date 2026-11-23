"""Tests del calendario de Eurex: festivos, vencimientos y tickers."""
from datetime import date, datetime, timezone

import pytest

import eurex_calendar as cal
from eurex_calendar import Contract, SPECS


def test_festivos_2026_oficiales():
    """Lista del Trading Calendar 2026 de Eurex (PDF oficial)."""
    esperado = {
        date(2026, 1, 1),   # Año Nuevo
        date(2026, 4, 3),   # Viernes Santo
        date(2026, 4, 6),   # Lunes de Pascua
        date(2026, 5, 1),   # Día del Trabajo
        date(2026, 12, 24),  # sin negociación
        date(2026, 12, 25),
        date(2026, 12, 31),  # sin negociación
    }
    assert cal.eurex_holidays(2026) == esperado


def test_pascua_conocida():
    assert cal.easter_sunday(2026) == date(2026, 4, 5)
    assert cal.easter_sunday(2027) == date(2027, 3, 28)
    assert cal.easter_sunday(2024) == date(2024, 3, 31)


def test_dias_de_negociacion():
    assert cal.is_trading_day(date(2026, 12, 28))          # lunes normal
    assert not cal.is_trading_day(date(2026, 12, 25))      # Navidad
    assert not cal.is_trading_day(date(2026, 12, 26))      # sábado
    assert cal.previous_trading_day(date(2026, 12, 25)) == date(2026, 12, 23)
    assert cal.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)


def test_vencimiento_mensual_rollback_por_festivo(monkeypatch):
    """Si el tercer viernes es festivo, el mensual pasa al día hábil anterior."""
    assert cal.monthly_expiry(2026, 12) == date(2026, 12, 18)
    monkeypatch.setitem(cal.HOLIDAY_OVERRIDES, 2026, {date(2026, 12, 18)})
    assert cal.monthly_expiry(2026, 12) == date(2026, 12, 17)


def test_semanales_de_diciembre_2026_no_incluyen_navidad():
    """Regresión: el código antiguo proponía el 25/12 como vencimiento semanal."""
    semanas = dict(cal.weekly_expiries(2026, 12))
    assert date(2026, 12, 4) in semanas
    assert date(2026, 12, 11) in semanas
    assert date(2026, 12, 18) not in semanas      # semana del mensual
    assert date(2026, 12, 25) not in semanas      # festivo


def test_ticker_mensual_y_semanal():
    assert Contract("DAX", date(2026, 11, 27), "weekly", 4).ticker == "ODX4"
    assert Contract("DAX", date(2026, 11, 20), "monthly").ticker == "ODAX"
    assert Contract("STOXX", date(2026, 11, 27), "weekly", 4).ticker == "OES4"
    assert Contract("STOXX", date(2026, 12, 18), "monthly").ticker == "OESX"


def test_hora_de_vencimiento_por_indice():
    dax = Contract("DAX", date(2026, 11, 27), "weekly", 4)
    stoxx = Contract("STOXX", date(2026, 11, 27), "weekly", 4)
    assert dax.expiry_dt.hour == 13
    assert stoxx.expiry_dt.hour == 12
    assert SPECS["DAX"].multiplier == 5.0
    assert SPECS["STOXX"].multiplier == 10.0
    assert SPECS["STOXX"].q == pytest.approx(0.03)


def test_dte_es_fraccion_de_dia_con_hora():
    now = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)   # 12:00 CET
    c = Contract("DAX", date(2026, 11, 27), "weekly", 4)       # vence 27 a las 13:00 CET
    assert c.days_to_expiry(now) == pytest.approx(4 + 1 / 24, abs=1e-6)


def test_select_contract_semanal_regresion_11_diciembre():
    now = datetime(2026, 12, 11, 10, 0, tzinfo=timezone.utc)  # 11:00 CET, aún negociable
    c = cal.select_contract("DAX", "weekly", now=now)
    assert c.expiry == date(2026, 12, 11) and c.ticker == "ODX2"


def test_select_contract_salta_vencimiento_inminente_y_navidad():
    now = datetime(2026, 12, 11, 12, 45, tzinfo=timezone.utc)  # 13:45 CET: ya venció
    c = cal.select_contract("DAX", "weekly", now=now)
    assert c.expiry == date(2027, 1, 8) and c.ticker == "ODX2"


def test_select_contract_mensual_y_saltos():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    assert cal.select_contract("DAX", "monthly", now=now, months_ahead=0).expiry == date(2026, 12, 18)
    assert cal.select_contract("DAX", "monthly", now=now, months_ahead=1).expiry == date(2027, 1, 15)
    assert cal.select_contract("DAX", "monthly", now=now, months_ahead=2).expiry == date(2027, 2, 19)


def test_sin_vencimiento_por_la_noche_no_falla():
    """En la mañana del vencimiento el contrato de hoy sigue siendo válido si falta >30 min."""
    now = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)   # 09:00 CET, vence 13:00 CET
    c = cal.select_contract("DAX", "weekly", now=now)
    assert c.expiry == date(2026, 11, 27)


def test_codigos_de_producto_coinciden_con_eurex():
    """Semanales = ODX*/OES*, mensuales = ODAX/OESX (Trading Calendar de Eurex)."""
    for index, weekly, monthly in (("DAX", "ODX", "ODAX"), ("STOXX", "OES", "OESX")):
        w = Contract(index, date(2026, 11, 27), "weekly", 4)
        m = Contract(index, date(2026, 12, 18), "monthly")
        assert w.ticker == f"{weekly}4"
        assert m.ticker == monthly
        weeklies = [Contract(index, f, "weekly", k).ticker
                    for f, k in cal.weekly_expiries(2026, 11)]
        assert all(t.startswith(weekly) for t in weeklies), weeklies
        assert not any(t.startswith(monthly) for t in weeklies), weeklies
