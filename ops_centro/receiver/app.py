"""Receiver de alertas do Grafana (RF06/RF07).

Fluxo: Grafana Cloud dispara webhook → este serviço valida o token, consulta contexto
adicional no Turso (correlação por trace_id, `receiver/enrichment.py`) e devolve a
mensagem enriquecida, que o envio ao Hermes/Telegram consome (issue #15).

Estado atual: auth por token compartilhado + enriquecimento (issue #14). O repasse ao
Hermes é a issue #15 — hoje o payload enriquecido sai no log estruturado e no corpo da
resposta, que é o que o teste de ponta a ponta consegue verificar.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from blu_observability_bootstrap import setup_observability, shutdown_observability
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from ops_centro import __version__
from ops_centro.conventions import APP_OPS_CENTRO, build_resource_attributes
from ops_centro.metrics import common_labels
from ops_centro.receiver.enrichment import enrich_alerts, summarize
from ops_centro.turso import shutdown_log_writer

logger = logging.getLogger(__name__)


class GrafanaAlert(BaseModel):
    """Um alerta individual dentro do payload de webhook do Grafana Alerting."""

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class GrafanaWebhookPayload(BaseModel):
    """Payload padrão do contact point 'webhook' do Grafana Alerting."""

    status: str
    title: str = ""
    message: str = ""
    alerts: list[GrafanaAlert] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Drena o writer de logs (RF05) antes de derrubar a telemetria: o que estiver
    # na fila ainda precisa do trace correspondente exportado.
    shutdown_log_writer()
    await shutdown_observability()


app = FastAPI(title="ops-centro receiver", version=__version__, lifespan=lifespan)

# Atributos comuns (RF02) — precisam ir para o Resource do OTel, senão o sinal chega
# no Grafana só com service.name e some das queries cruzadas por app_name/environment.
RESOURCE_ATTRIBUTES = build_resource_attributes(APP_OPS_CENTRO, version=__version__)

# OTLP para o próprio receiver (o observador também é observado). Sem
# OTEL_EXPORTER_OTLP_ENDPOINT configurado, vira no-op silencioso — determinístico
# em teste/CI. Langfuse não se aplica a este serviço.
setup_observability(
    app, APP_OPS_CENTRO, langfuse=False, resource_attributes=RESOURCE_ATTRIBUTES
)


def _expected_token() -> str | None:
    return os.environ.get("ALERT_WEBHOOK_TOKEN") or None


# `ops_centro_alerts_received_total` do catálogo da §7 (ops_centro/metrics.py). O
# instrumento é criado na primeira chamada porque o MeterProvider só existe depois do
# setup_observability acima — e sem OTLP configurado ele é um no-op do próprio SDK.
_alerts_counter: object | None = None


def _record_alert(alert_status: str) -> None:
    """Conta um webhook aceito.

    O label `status` segue o vocabulário congelado do schema (`ok` | `error`), não o
    `firing`/`resolved` do Grafana: alerta disparando é a condição de erro, alerta
    resolvido é o retorno ao normal. Métrica é observação de saúde, não eco do payload.
    """
    global _alerts_counter
    try:
        if _alerts_counter is None:
            from opentelemetry import metrics as otel_metrics

            _alerts_counter = otel_metrics.get_meter("ops_centro.receiver").create_counter(
                "ops_centro_alerts_received_total",
                description="Alertas recebidos do Grafana pelo receiver (RF06)",
            )
        # `app_name`/`environment` como atributo do ponto: o Grafana Cloud não promove
        # resource attribute a label de métrica (ops_centro.metrics.common_labels).
        _alerts_counter.add(
            1,
            {
                **common_labels(APP_OPS_CENTRO),
                "status": "ok" if alert_status == "resolved" else "error",
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba o webhook
        logger.debug("falha ao contar o alerta recebido: %s", exc)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "app": APP_OPS_CENTRO, "version": __version__}


def _presented_token(x_alert_token: str | None, authorization: str | None) -> str | None:
    """Token apresentado pelo chamador, nos dois formatos aceitos.

    `X-Alert-Token` é o do contrato da issue #12 e o que se usa num `curl` de teste.
    `Authorization: Bearer` existe porque o contact point `webhook` do Grafana suporta
    esse esquema em qualquer versão, enquanto headers customizados só nas mais novas — o
    contact point as-code manda os dois (grafana/alerts/roteamento.yaml).
    """
    if x_alert_token:
        return x_alert_token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


@app.post("/alerts/grafana", status_code=status.HTTP_202_ACCEPTED)
async def receive_grafana_alert(
    payload: GrafanaWebhookPayload,
    x_alert_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recebe o webhook do Grafana Alerting, enriquece e devolve (RF06/RF07).

    Auth: token compartilhado, configurado também no contact point do Grafana
    (RNF06: token vem de env var, nunca de código).
    """
    expected = _expected_token()
    if expected is None:
        # Sem token configurado o endpoint é inoperante de propósito: falhar
        # fechado evita aceitar webhook não autenticado em produção.
        raise HTTPException(status_code=503, detail="ALERT_WEBHOOK_TOKEN não configurado")
    apresentado = _presented_token(x_alert_token, authorization)
    if not apresentado or not secrets.compare_digest(apresentado, expected):
        raise HTTPException(status_code=401, detail="token inválido")

    logger.info(
        "alerta recebido do Grafana",
        extra={"alert_status": payload.status, "alert_count": len(payload.alerts)},
    )
    _record_alert(payload.status)

    # Enriquecimento com contexto do Turso (issue #14). Tem deadline próprio e nunca
    # levanta: alerta sem contexto ainda é alerta, alerta atrasado não é.
    enriquecidos = await enrich_alerts(payload.alerts)
    resumo = summarize(enriquecidos)
    logger.info("alerta enriquecido", extra=resumo)

    # TODO(#15): repassar `alerts` ao Hermes, que formata e manda no Telegram.
    return {"result": "accepted", **resumo, "alerts_detail": [e.as_dict() for e in enriquecidos]}
