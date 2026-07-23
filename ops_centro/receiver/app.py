"""Receiver de alertas do Grafana (RF06/RF07).

Fluxo alvo: Grafana Cloud dispara webhook → este serviço valida o token, consulta
contexto adicional no Turso (correlação por trace_id) e repassa mensagem
enriquecida ao Hermes, que notifica o Telegram.

Estado atual: skeleton com auth por token compartilhado e persistência do fluxo
pendente nas issues (enriquecimento via Turso, envio ao Hermes). O endpoint já
aceita o payload padrão de webhook do Grafana Alerting.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager

from blu_observability_bootstrap import setup_observability, shutdown_observability
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from ops_centro import __version__
from ops_centro.conventions import APP_OPS_CENTRO, build_resource_attributes
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

# OTLP para o próprio receiver (o observador também é observado). Sem
# OTEL_EXPORTER_OTLP_ENDPOINT configurado, vira no-op silencioso — determinístico
# em teste/CI. Langfuse não se aplica a este serviço.
setup_observability(app, APP_OPS_CENTRO, langfuse=False)

# Atributos comuns (RF02) anexados ao contexto de log deste app.
RESOURCE_ATTRIBUTES = build_resource_attributes(APP_OPS_CENTRO, version=__version__)


def _expected_token() -> str | None:
    return os.environ.get("ALERT_WEBHOOK_TOKEN") or None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "app": APP_OPS_CENTRO, "version": __version__}


@app.post("/alerts/grafana", status_code=status.HTTP_202_ACCEPTED)
async def receive_grafana_alert(
    payload: GrafanaWebhookPayload,
    x_alert_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Recebe o webhook do Grafana Alerting (RF06).

    Auth: token compartilhado no header X-Alert-Token, configurado também no
    contact point do Grafana (RNF06: token vem de env var, nunca de código).
    """
    expected = _expected_token()
    if expected is None:
        # Sem token configurado o endpoint é inoperante de propósito: falhar
        # fechado evita aceitar webhook não autenticado em produção.
        raise HTTPException(status_code=503, detail="ALERT_WEBHOOK_TOKEN não configurado")
    if not x_alert_token or not secrets.compare_digest(x_alert_token, expected):
        raise HTTPException(status_code=401, detail="token inválido")

    logger.info(
        "alerta recebido do Grafana",
        extra={"alert_status": payload.status, "alert_count": len(payload.alerts)},
    )

    # TODO(fase 3): enriquecer com contexto do Turso (trace_id) e enviar ao Hermes.
    return {"result": "accepted", "alerts": str(len(payload.alerts))}
