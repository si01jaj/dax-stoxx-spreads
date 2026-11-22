"""Tests de la capa HTTP: validación y estado, sin llamar al LLM ni a Yahoo."""
import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture(scope="module")
def client():
    return TestClient(api.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_analyze_rechaza_vencimiento_invalido(client):
    # Antes cualquier valor desconocido caía silenciosamente en el mensual.
    r = client.post("/api/analyze", json={"expiry_type": "trimestral"})
    assert r.status_code == 422


def test_analyze_rechaza_precios_absurdos(client):
    r = client.post("/api/analyze", json={"dax_price": -5})
    assert r.status_code == 422


def test_chat_sin_analisis_previo_da_400(client):
    api._context.clear()
    r = client.post("/api/chat", json={"message": "¿qué hago?"})
    assert r.status_code == 400


def test_chat_rechaza_mensaje_vacio(client):
    r = client.post("/api/chat", json={"message": ""})
    assert r.status_code == 422


def test_contexto_acotado():
    api._context.clear()
    for i in range(api.MAX_CONTEXT_TURNS + 10):
        api._context.append(f"turno {i} " + "x" * 1000)
    texto = api._context_text()
    assert len(texto) <= api.MAX_CONTEXT_CHARS
    assert len(api._context) == api.MAX_CONTEXT_TURNS
