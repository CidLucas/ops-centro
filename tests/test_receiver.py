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


def test_webhook_reporta_a_entrega_ao_hermes(client, monkeypatch):
    """Sem Hermes configurado o webhook segue respondendo 202 — e diz que não enviou
    (issue #15: o estado do canal é observável na própria resposta)."""
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    monkeypatch.delenv("HERMES_WEBHOOK_URL", raising=False)
    resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "segredo"})
    assert resp.status_code == 202
    corpo = resp.json()
    assert corpo["delivery"]["status"] == "desativado"
    assert corpo["actions"] == []  # alerta sem label `tool` não dispara ação (#17)


def test_webhook_payload_invalido(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    resp = client.post(
        "/alerts/grafana", json={"sem": "status"}, headers={"X-Alert-Token": "segredo"}
    )
    assert resp.status_code == 422


def test_webhook_conta_a_metrica_do_proprio_receiver(client, monkeypatch):
    """`ops_centro_alerts_received_total` do catálogo da §7 — o observador também é
    observado. `firing` conta como `error` no vocabulário congelado do schema."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from ops_centro.receiver import app as receiver

    leitor = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[leitor])
    monkeypatch.setattr(receiver, "_alerts_counter", None)
    monkeypatch.setattr(
        "opentelemetry.metrics.get_meter", lambda *a, **k: provider.get_meter("teste")
    )
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")

    resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "segredo"})
    assert resp.status_code == 202

    pontos = [
        ponto
        for recurso in leitor.get_metrics_data().resource_metrics
        for escopo in recurso.scope_metrics
        for metrica in escopo.metrics
        if metrica.name == "ops_centro_alerts_received_total"
        for ponto in metrica.data.data_points
    ]
    assert [(p.value, p.attributes["status"]) for p in pontos] == [(1, "error")]
