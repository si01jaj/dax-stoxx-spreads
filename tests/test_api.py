"""Tests de la API: validación, errores, auth y ausencia de bloqueo."""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api
import main as engine
import marketdata
from eurex_calendar import Contract

BODY = {
    "indices": ["DAX", "STOXX"],
    "expiry_type": "semanal",
    "dax_price": 25000.0,
    "dax_iv": 0.20,
    "stoxx_price": 6000.0,
    "stoxx_iv": 0.20,
    "narrative": False,
}


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    """Ningún test toca Yahoo: se fijan spot e IV manualmente por petición."""
    marketdata.clear_cache()
    monkeypatch.setattr(api, "APP_TOKEN", "")
    yield
    marketdata.clear_cache()


@pytest.fixture
def client():
    return TestClient(api.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"] == api.VERSION
    assert r.json()["status"] == "ok"


def test_analyze_devuelve_candidatos_coherentes(client):
    r = client.post("/api/analyze", json=BODY)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert set(payload["contracts"]) == {"DAX", "STOXX"}
    rows = payload["candidates"]["DAX"]
    assert rows, "debería haber alguna estructura aceptada"
    for row in rows:
        width = row["width_pts"]
        credit = row["credit_pts"]
        assert row["max_loss_eur"] == pytest.approx((width - credit) * 5, abs=0.1)
        assert row["max_profit_eur"] == pytest.approx(credit * 5, abs=0.1)
        assert 0.0 < row["pop"] < 1.0
        assert row["roc"] == pytest.approx(credit / (width - credit), abs=1e-3)
        assert row["accepted"] is True
        for leg in row["legs"]:
            assert leg["strike"] % 50 == 0


def test_iv_manual_se_respeta_y_se_marca_como_tal(client):
    body = {**BODY, "dax_iv": 0.256}
    payload = client.post("/api/analyze", json=body).json()
    market = payload["markets"]["DAX"]
    assert market["atm_iv"]["value"] == pytest.approx(0.256)
    assert market["atm_iv"]["source"] == "manual"


def test_validacion_de_rangos(client):
    for bad in (
        {"dax_price": -1},
        {"dax_price": 0},
        {"dax_iv": 0},
        {"dax_iv": 10},
        {"expiry_type": "anual"},
        {"indices": []},
    ):
        r = client.post("/api/analyze", json={**BODY, **bad})
        assert r.status_code == 422, f"{bad} debería ser rechazado"


def test_sin_iv_y_sin_fallback_devuelve_422(client, monkeypatch):
    """Sin datos de Yahoo y sin IV manual: error explícito, nunca 20% silencioso."""
    monkeypatch.setattr(marketdata, "get_spot", lambda index, override=None: marketdata.Quote(25000.0, "manual") if index == "DAX" else marketdata.Quote(6000.0, "manual"))
    monkeypatch.setattr(marketdata, "get_atm_iv", lambda index, override=None: marketdata.Quote(None, "missing", note="sin IV"))
    monkeypatch.setattr(marketdata, "ALLOW_IV_FALLBACK", False)
    marketdata.clear_cache()
    r = client.post("/api/analyze", json={"expiry_type": "semanal", "narrative": False})
    assert r.status_code == 422
    assert "IV" in str(r.json()["detail"])


def test_fallback_iv_marcado_como_asumido(client, monkeypatch):
    monkeypatch.setattr(marketdata, "get_spot", lambda index, override=None: marketdata.Quote(25000.0 if index == "DAX" else 6000.0, "manual"))
    monkeypatch.setattr(marketdata, "get_atm_iv", lambda index, override=None: marketdata.Quote(None, "missing"))
    monkeypatch.setattr(marketdata, "ALLOW_IV_FALLBACK", True)
    marketdata.clear_cache()
    payload = client.post("/api/analyze", json={"indices": ["DAX"], "narrative": False}).json()
    assert payload["markets"]["DAX"]["atm_iv"]["source"] == "assumed"
    assert any("ASUMIDA" in w for w in payload["warnings"])


def test_auth_por_token(client, monkeypatch):
    monkeypatch.setattr(api, "APP_TOKEN", "secreto")
    assert client.get("/api/defaults").status_code == 401
    r = client.post("/api/analyze", json=BODY, headers={"X-App-Token": "secreto"})
    assert r.status_code == 200
    assert client.get("/api/health").status_code == 200      # health siempre abierto


def test_chat_sin_analisis_devuelve_409(client):
    r = client.post("/api/chat", json={"message": "hola", "session_id": "x"})
    assert r.status_code == 409


def test_chat_sin_api_key_devuelve_503(client, monkeypatch):
    client.post("/api/analyze", json={**BODY, "session_id": "s1"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    r = client.post("/api/chat", json={"message": "hola", "session_id": "s1"})
    assert r.status_code == 503


def test_sesiones_acotadas():
    api._SESSIONS.clear()
    for i in range(api.MAX_SESSIONS + 20):
        api._session(f"s{i}")
    assert len(api._SESSIONS) <= api.MAX_SESSIONS


def test_defaults_devuelve_precio_aunque_falte_la_iv(client, monkeypatch):
    """El formulario debe precargar el precio aunque la IV no esté disponible."""
    monkeypatch.setattr(marketdata, "get_spot", lambda index, override=None: marketdata.Quote(25000.0, "manual"))
    monkeypatch.setattr(marketdata, "get_atm_iv", lambda index, override=None: marketdata.Quote(None, "missing", note="sin IV en Yahoo"))
    marketdata.clear_cache()
    r = client.get("/api/defaults")
    assert r.status_code == 200
    data = r.json()
    assert data["markets"]["DAX"]["spot"]["value"] == 25000.0
    assert data["markets"]["DAX"]["atm_iv"]["value"] is None
    assert any("DAX" in w and "IV" in w for w in data["warnings"])
    assert "DAX" in data["contracts"]
