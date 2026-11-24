"""Tests de la capa HTTP (api.py): validación y estado, sin red ni LLM."""
import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture(scope="module")
def client():
    return TestClient(api.app)


@pytest.fixture(autouse=True)
def sin_token(monkeypatch):
    """El token es opcional (solo si APP_TOKEN está definido)."""
    monkeypatch.setattr(api, "APP_TOKEN", "")


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["auth_required"] is False


def test_analyze_rechaza_vencimiento_invalido(client):
    r = client.post("/api/analyze", json={"expiry_type": "trimestral"})
    assert r.status_code == 422


def test_analyze_rechaza_precio_negativo(client):
    r = client.post("/api/analyze", json={"dax_price": -5})
    assert r.status_code == 422


def test_analyze_rechaza_precio_absurdo(client):
    r = client.post("/api/analyze", json={"dax_price": 5_000_000})
    assert r.status_code == 422


def test_analyze_rechaza_iv_fuera_de_rango(client):
    r = client.post("/api/analyze", json={"dax_iv": 5.0})
    assert r.status_code == 422


def test_analyze_rechaza_indice_desconocido(client):
    r = client.post("/api/analyze", json={"indices": ["IBEX"]})
    assert r.status_code == 422


def test_analyze_rechaza_pop_imposible(client):
    r = client.post("/api/analyze", json={"min_pop": 0.2})
    assert r.status_code == 422


def test_el_dashboard_sirve_el_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "favicon.svg" in r.text


def test_los_favicons_estan_disponibles(client):
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/favicon.ico").status_code == 200
