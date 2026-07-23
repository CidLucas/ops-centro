"""Testes do receiver de alertas (auth por token + contrato do webhook)."""

import pytest
from fastapi.testclient import TestClient

from ops_centro.receiver.app import app

pytestmark = pytest.mark.unit

PAYLOAD = {
    "status": "firing",
    "title": "Taxa de erro alta",
    "message": "erro > 5% no agents-platform",
    "alerts": [
        {
            "status": "firing",
            "labels": {"app_name": "agents-platform", "tenant_id": "acme"},
            "annotations": {"summary": "taxa de erro acima do limiar"},
        }
    ],
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "ops-centro"


def test_webhook_sem_token_configurado_falha_fechado(client, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    resp = client.post("/alerts/grafana", json=PAYLOAD)
    assert resp.status_code == 503


def test_webhook_token_invalido(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "errado"})
    assert resp.status_code == 401


def test_webhook_sem_header(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    resp = client.post("/alerts/grafana", json=PAYLOAD)
    assert resp.status_code == 401


def test_webhook_aceito(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "segredo"})
    assert resp.status_code == 202
    assert resp.json()["result"] == "accepted"


def test_webhook_payload_invalido(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    resp = client.post(
        "/alerts/grafana", json={"sem": "status"}, headers={"X-Alert-Token": "segredo"}
    )
    assert resp.status_code == 422
