"""Tests del modo manual: sin red, sin datos inventados."""
import pytest

import main as engine
import marketdata


@pytest.fixture(autouse=True)
def _limpia(monkeypatch):
    marketdata.clear_cache()
    monkeypatch.setattr(marketdata, "IV_PROVIDER", "none")
    monkeypatch.setattr(marketdata, "SPOT_PROVIDER", "manual")
    yield
    marketdata.clear_cache()


def _sin_red(*args, **kwargs):
    raise AssertionError("no debe haber peticiones de red en modo manual")


def test_iv_manual_no_sale_a_la_red(monkeypatch):
    monkeypatch.setattr(marketdata, "_yahoo_last", _sin_red)
    quote = marketdata.get_atm_iv("DAX")
    assert quote.value is None
    assert "bróker" in quote.note


def test_spot_manual_no_sale_a_la_red(monkeypatch):
    monkeypatch.setattr(marketdata, "_yahoo_last", _sin_red)
    quote = marketdata.get_spot("DAX")
    assert quote.value is None
    assert quote.source == "missing"


def test_override_no_sale_a_la_red(monkeypatch):
    monkeypatch.setattr(marketdata, "_yahoo_last", _sin_red)
    assert marketdata.get_spot("DAX", 25000).value == 25000
    assert marketdata.get_atm_iv("DAX", 0.256).value == pytest.approx(0.256)
    assert marketdata.get_atm_iv("DAX", 0.256).source == "manual"


def test_analisis_completo_offline(monkeypatch):
    """Con precio e IV a mano el motor funciona sin ninguna conexión."""
    monkeypatch.setattr(marketdata, "_yahoo_last", _sin_red)
    payload = engine.run_analysis(
        indices=("DAX", "STOXX"),
        expiry_type="semanal",
        spot_overrides={"DAX": 25000.0, "STOXX": 6000.0},
        iv_overrides={"DAX": 0.256, "STOXX": 0.22},
        use_llm=False,
    )
    assert not payload["errors"]
    assert payload["markets"]["DAX"]["spot"]["source"] == "manual"
    assert payload["markets"]["DAX"]["atm_iv"]["source"] == "manual"
    assert payload["markets"]["STOXX"]["atm_iv"]["value"] == pytest.approx(0.22)
    assert payload["candidates"]["DAX"], "debe haber estructuras con esos datos"


def test_sin_iv_falla_en_vez_de_asumir(monkeypatch):
    monkeypatch.setattr(marketdata, "ALLOW_IV_FALLBACK", False)
    payload = engine.run_analysis(
        indices=("DAX",),
        expiry_type="semanal",
        spot_overrides={"DAX": 25000.0},
        use_llm=False,
    )
    assert "DAX" in payload["errors"]
    assert "IV" in payload["errors"]["DAX"]
    assert payload["candidates"].get("DAX") in (None, [])


def test_cli_acepta_iv_en_porcentaje():
    assert engine._parse_pairs(["DAX=25.6"], percent=True) == {"DAX": pytest.approx(0.256)}
    assert engine._parse_pairs(["DAX=0.256"], percent=True) == {"DAX": pytest.approx(0.256)}
    assert engine._parse_pairs(["DAX=25,6%"], percent=True) == {"DAX": pytest.approx(0.256)}
    assert engine._parse_pairs(["STOXX=6000"]) == {"STOXX": 6000.0}


def test_cli_rechaza_indice_desconocido():
    with pytest.raises(ValueError):
        engine._parse_pairs(["SPX=5000"])
    with pytest.raises(ValueError):
        engine._parse_pairs(["DAX"])
