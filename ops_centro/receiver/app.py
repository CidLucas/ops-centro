"""Receiver de alertas do Grafana e canal do Hermes (RF06/RF07/RF08/RF09).

Fluxo do alerta: Grafana Cloud dispara webhook → este serviço valida o token, consulta
contexto adicional no Turso (correlação por trace_id, `receiver/enrichment.py`), entrega a
notificação ao Hermes (`receiver/hermes.py`, issue #15) e, se a evidência justificar, toma
a ação autônoma da RF09 (`receiver/actions.py`, issue #17).

O caminho inverso também passa por aqui: `POST /hermes/consulta` responde as perguntas de
status vindas do Telegram (`receiver/status.py`, RF08 / issues #16 e #19) e
`POST /hermes/confirmacao` fecha o laço das ações de maior impacto — proposta com botões,
confirmação humana, execução (`receiver/confirmations.py`, RF10 / issue #18).

Os três endpoints usam o **mesmo padrão de auth**: token compartilhado em `X-Alert-Token`
ou `Authorization: Bearer`, comparado com `secrets.compare_digest`, falhando fechado quando
não há token configurado. No caso da confirmação, esse token só prova que quem chamou é o
Hermes; **quem** pode confirmar é a allowlist de `chat_id` do `confirmations.py`.
"""

from __future__ import annotations

import asyncio
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
from ops_centro.receiver import actions, heartbeat
from ops_centro.receiver.confirmations import confirm
from ops_centro.receiver.enrichment import enrich_alerts, summarize
from ops_centro.receiver.hermes import notify_alerts
from ops_centro.receiver.status import run_command
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


class ConsultaPayload(BaseModel):
    """Consulta sob demanda vinda do Hermes (RF08, issue #16): o texto cru do Telegram.

    `chat_id`/`user` só importam para os comandos de ação (#18) — quem pode propor um
    restart é decidido pelo chat, não pelo texto da mensagem.
    """

    command: str = ""
    chat_id: str = ""
    user: str = ""


class ConfirmacaoPayload(BaseModel):
    """Toque num botão inline da proposta (RF10, issue #18).

    O Hermes repassa o `callback_data` do botão como veio; `token`/`decision` existem para
    quem preferir montar a chamada à mão (é o que a CLI e o `curl` do roteiro fazem).
    """

    callback_data: str = ""
    token: str = ""
    decision: str = ""
    chat_id: str = ""
    user: str = ""
    message_id: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _heartbeat_task
    # Pausa cujo TTL venceu enquanto o receiver estava fora precisa ser desfeita ao voltar:
    # o estado mora no Turso (issue #17), então um restart não deixa tool pausada para
    # sempre. Sem Turso configurado, é no-op.
    await actions.sweep_expired()
    # Heartbeat (issue #27): o receiver passa a emitir série contínua — é isso que dá NoData
    # para a regra `ops-centro-host-deadman` observar quando a EC2 emudece. Sem OTLP
    # configurado, vira no-op silencioso (mesma filosofia do resto da telemetria).
    _heartbeat_task = heartbeat.start(heartbeat._interval_seconds())
    yield
    # Drena o writer de logs (RF05) antes de derrubar a telemetria: o que estiver
    # na fila ainda precisa do trace correspondente exportado. O heartbeat para ANTES do
    # shutdown_observability, para o último batimento ser exportado.
    heartbeat.stop(_heartbeat_task)
    shutdown_log_writer()
    await shutdown_observability()


app = FastAPI(title="ops-centro receiver", version=__version__, lifespan=lifespan)

# Task do heartbeat (issue #27), criada no start do lifespan e cancelada no shutdown. Fica
# como estado de módulo para o teste conseguir inspecionar (mesmo padrão do `_alerts_counter`).
_heartbeat_task: asyncio.Task | None = None

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


def _require_token(x_alert_token: str | None, authorization: str | None) -> None:
    """Auth compartilhada pelos endpoints do Hermes (RNF06: token vem de env var).

    Sem token configurado o endpoint é inoperante de propósito: falhar fechado evita
    aceitar chamada não autenticada em produção.
    """
    expected = _expected_token()
    if expected is None:
        raise HTTPException(status_code=503, detail="ALERT_WEBHOOK_TOKEN não configurado")
    apresentado = _presented_token(x_alert_token, authorization)
    if not apresentado or not secrets.compare_digest(apresentado, expected):
        raise HTTPException(status_code=401, detail="token inválido")


@app.post("/alerts/grafana", status_code=status.HTTP_202_ACCEPTED)
async def receive_grafana_alert(
    payload: GrafanaWebhookPayload,
    x_alert_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recebe o webhook do Grafana Alerting, enriquece, notifica e age (RF06/RF07/RF09)."""
    _require_token(x_alert_token, authorization)

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

    # Entrega ao Hermes → Telegram (issue #15). Tem retry, rate limit e dead-letter
    # próprios e nunca levanta: alerta não entregue vira registro, não erro no webhook.
    entrega = await notify_alerts(enriquecidos)

    # Ação autônoma de baixo risco (RF09, issue #17): só age com evidência confirmada no
    # Turso e com o kill switch desligado. Também nunca levanta.
    executadas = await actions.handle_alerts(enriquecidos)
    # Pausa vencida é desfeita aqui além do start: o webhook é o batimento mais frequente
    # que este serviço tem, e depender só do restart deixaria TTL vencido em aberto.
    await actions.sweep_expired()

    return {
        "result": "accepted",
        **resumo,
        "delivery": entrega.as_dict(),
        "actions": executadas,
        "alerts_detail": [e.as_dict() for e in enriquecidos],
    }


@app.post("/hermes/consulta")
async def hermes_consulta(
    payload: ConsultaPayload,
    x_alert_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Responde uma consulta sob demanda do Telegram (RF08, issue #16).

    Corpo: `{"command": "/status acme"}`. A resposta traz `text` já em MarkdownV2 (o
    Hermes só repassa) e `data` estruturado. Comando desconhecido vira `/ajuda` com 200 —
    quem digitou errado no Telegram quer a lista de comandos, não um código de erro.
    """
    _require_token(x_alert_token, authorization)
    resposta = await run_command(payload.command, chat_id=payload.chat_id, user=payload.user)
    logger.info(
        "consulta respondida",
        extra={"comando": resposta["command"], "bruto": payload.command[:120]},
    )
    return resposta


@app.post("/hermes/confirmacao")
async def hermes_confirmacao(
    payload: ConfirmacaoPayload,
    x_alert_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Executa (ou recusa) uma ação proposta, depois do botão no Telegram (RF10, issue #18).

    Sempre **200**, inclusive quando a confirmação é recusada: o corpo traz `status` e o
    `text` que explica o motivo, e o Hermes só precisa repassar a mensagem ao chat. Um 4xx
    aqui viraria "o bot não respondeu" na tela de quem apertou o botão — que é a pior
    resposta possível para quem acabou de pedir um restart e não sabe se ele aconteceu.
    """
    _require_token(x_alert_token, authorization)
    resultado = await confirm(
        token=payload.token,
        callback=payload.callback_data,
        decision=payload.decision,
        chat_id=payload.chat_id,
        user=payload.user,
    )
    logger.info(
        "confirmação processada",
        extra={"status": resultado.status, "action": resultado.action,
               "target": resultado.target},
    )
    return resultado.as_response(payload.message_id)
