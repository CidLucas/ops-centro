"""Enriquecimento de alertas com contexto do Turso (RF07 — parte 1, issue #14).

O critério de sucesso §12 do plano é explícito: o alerta tem de chegar com contexto útil,
não com "algo deu errado". Este módulo é o que transforma uma linha de limiar ultrapassado
em uma mensagem que já responde as três primeiras perguntas de quem foi acordado:

- **o que quebrou** — resumo e severidade vindos das annotations da regra (#12);
- **quem sentiu** — `app_name` e `tenant_id` das labels do alerta;
- **o que apareceu no log** — as últimas linhas correlacionadas no Turso, por `trace_id`
  quando o alerta traz um, ou pela janela recente do app/tenant quando não traz
  (`ops_centro.turso.log_reader`).

Mais dois links prontos: o trace no Tempo e o dashboard do app, ambos já filtrados.

**Regra inegociável (critério de aceite do #14): o enriquecimento nunca segura o alerta.**
A consulta roda numa thread com deadline (`ALERT_ENRICHMENT_TIMEOUT`, default 2s) e
qualquer falha — Turso fora do ar, timeout, tabela ausente — devolve o alerta sem contexto,
marcado com o motivo. Alerta pobre chega; alerta atrasado não é alerta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import quote

from ops_centro.conventions import (
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    APP_OPS_CENTRO,
    ATTR_APP_NAME,
    ATTR_ENVIRONMENT,
    ATTR_TENANT_ID,
)
from ops_centro.metrics import common_labels
from ops_centro.turso.connection import connect, is_configured
from ops_centro.turso.log_reader import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    LogLine,
    related_logs,
)

logger = logging.getLogger(__name__)

# Deadline da consulta ao Turso, em segundos. 2s é folgado para uma leitura por índice e
# curto o bastante para o Grafana não considerar o webhook lento.
DEFAULT_TIMEOUT_SECONDS = 2.0

# Teto de alertas enriquecidos por webhook. O contact point manda até 20 por notificação
# (maxAlerts em grafana/alerts/roteamento.yaml); enriquecer todos multiplicaria a consulta
# sem melhorar a mensagem — as primeiras já dizem o que está acontecendo.
DEFAULT_MAX_ALERTS = 5

# Chaves de label onde o trace pode vir. `trace_id` é o do schema (RF02); as outras duas
# aparecem quando o alerta nasce de uma anotação ou de um exemplar do Prometheus.
TRACE_LABEL_KEYS = ("trace_id", "traceID", "traceId")

# Dashboard as-code (#10) correspondente a cada app — o link que vai na mensagem.
DASHBOARD_BY_APP = {
    APP_AGENTS_PLATFORM: "ops-centro-agents-platform",
    APP_FILE_MEMORY: "ops-centro-file-memory",
    APP_OPS_CENTRO: "ops-centro-visao-geral",
}
DEFAULT_DASHBOARD = "ops-centro-visao-geral"

# Estratégias de enriquecimento (o que a mensagem diz de onde veio o contexto).
STRATEGY_TRACE = "trace"
STRATEGY_WINDOW = "janela"
STRATEGY_NONE = "nenhuma"
STRATEGY_DISABLED = "desativado"
STRATEGY_UNAVAILABLE = "indisponível"


class HasAlertFields(Protocol):
    """O que o enriquecimento precisa de um alerta (ver `GrafanaAlert` em app.py).

    Protocolo em vez de import direto para o receiver não virar dependência circular do
    módulo que ele mesmo usa.
    """

    status: str
    labels: dict[str, str]
    annotations: dict[str, str]


# --- contexto extraído do alerta ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class AlertContext:
    """O que dá para saber a partir do próprio alerta, antes de consultar qualquer coisa."""

    alertname: str = ""
    status: str = ""
    severity: str = ""
    app_name: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    environment: str | None = None
    summary: str = ""
    description: str = ""
    runbook_url: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_alert(cls, alert: HasAlertFields) -> "AlertContext":
        labels = dict(alert.labels or {})
        annotations = dict(alert.annotations or {})
        return cls(
            alertname=labels.get("alertname", ""),
            status=alert.status,
            severity=labels.get("severity", ""),
            app_name=labels.get(ATTR_APP_NAME) or None,
            tenant_id=labels.get(ATTR_TENANT_ID) or None,
            # O trace pode vir da label (schema, RF02) ou de uma annotation — o Grafana
            # copia exemplars para lá em algumas integrações.
            trace_id=_first(labels, TRACE_LABEL_KEYS) or _first(annotations, TRACE_LABEL_KEYS),
            environment=labels.get(ATTR_ENVIRONMENT) or None,
            summary=annotations.get("summary", ""),
            description=annotations.get("description", ""),
            runbook_url=annotations.get("runbook_url", ""),
            labels=labels,
        )

    def headline(self) -> str:
        """Primeira linha da mensagem: o que houve, com quem, em que estado."""
        alvo = " · ".join(filter(None, (self.app_name, self.tenant_id, self.environment)))
        titulo = self.summary or self.alertname or "alerta sem título"
        estado = (self.status or "").upper()
        return f"[{estado}{'/' + self.severity if self.severity else ''}] {titulo}" + (
            f" ({alvo})" if alvo else ""
        )


def _first(origem: dict[str, str], chaves: Sequence[str]) -> str | None:
    for chave in chaves:
        valor = (origem.get(chave) or "").strip()
        if valor:
            return valor
    return None


# --- links -------------------------------------------------------------------------
def _stack_url() -> str:
    return os.environ.get("GRAFANA_STACK_URL", "").rstrip("/")


def trace_url(trace_id: str, *, stack_url: str | None = None) -> str:
    """Link direto para o trace no Tempo (Explore).

    Formato `panes` do Explore (schemaVersion=1) — o `left=` antigo ainda funciona em
    algumas versões, mas é o que quebra primeiro em upgrade de Grafana.
    """
    stack = stack_url if stack_url is not None else _stack_url()
    if not (stack and trace_id):
        return ""
    datasource = os.environ.get("GRAFANA_TEMPO_DS_UID", "grafanacloud-traces")
    pane = {
        "trace": {
            "datasource": datasource,
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "tempo", "uid": datasource},
                    "queryType": "traceql",
                    "query": trace_id,
                }
            ],
            "range": {"from": "now-6h", "to": "now"},
        }
    }
    panes = quote(json.dumps(pane, separators=(",", ":")))
    return f"{stack}/explore?schemaVersion=1&orgId=1&panes={panes}"


def dashboard_url(app_name: str | None, environment: str | None = None,
                  *, stack_url: str | None = None) -> str:
    """Link para o dashboard as-code do app, já filtrado pelo ambiente do alerta."""
    stack = stack_url if stack_url is not None else _stack_url()
    if not stack:
        return ""
    uid = DASHBOARD_BY_APP.get(app_name or "", DEFAULT_DASHBOARD)
    filtro = f"&var-environment={quote(environment)}" if environment else ""
    return f"{stack}/d/{uid}?from=now-3h&to=now{filtro}"


# --- resultado ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EnrichedAlert:
    """Um alerta com o contexto que foi possível juntar dentro do deadline."""

    context: AlertContext
    logs: tuple[LogLine, ...] = ()
    strategy: str = STRATEGY_NONE
    reason: str = ""

    @property
    def enriched(self) -> bool:
        return bool(self.logs)

    def links(self) -> dict[str, str]:
        links = {"dashboard": dashboard_url(self.context.app_name, self.context.environment)}
        if self.context.trace_id:
            links["trace"] = trace_url(self.context.trace_id)
        if self.context.runbook_url:
            links["runbook"] = self.context.runbook_url
        return {chave: valor for chave, valor in links.items() if valor}

    def as_dict(self) -> dict[str, Any]:
        """Payload que o envio ao Telegram (#15) vai consumir."""
        return {
            "alertname": self.context.alertname,
            "status": self.context.status,
            "severity": self.context.severity,
            "app_name": self.context.app_name,
            "tenant_id": self.context.tenant_id,
            "trace_id": self.context.trace_id,
            "environment": self.context.environment,
            "summary": self.context.summary,
            "description": self.context.description,
            "strategy": self.strategy,
            "reason": self.reason,
            "logs": [linha.as_dict() for linha in self.logs],
            "links": self.links(),
        }

    def as_text(self) -> str:
        """A mesma coisa em texto — é o corpo da mensagem do Telegram (#15)."""
        partes = [self.context.headline()]
        if self.context.description:
            partes.append(self.context.description.strip())
        if self.logs:
            origem = (
                f"trace {self.context.trace_id}"
                if self.strategy == STRATEGY_TRACE
                else f"janela recente de {self.context.app_name}"
            )
            partes.append(f"Últimos logs ({origem}):")
            partes.extend(f"  {linha.one_line()}" for linha in self.logs)
        else:
            partes.append(f"Sem logs correlacionados ({self.reason or self.strategy}).")
        partes.extend(f"{nome}: {url}" for nome, url in self.links().items())
        return "\n".join(partes)


# --- métricas do próprio enriquecimento -----------------------------------------------
# Instrumentos criados na primeira chamada: o MeterProvider só existe depois do
# setup_observability do app (mesmo motivo do contador em receiver/app.py).
_enrichment_counter: Any = None
_enrichment_histogram: Any = None


def _record(status: str, duracao: float) -> None:
    """Conta e cronometra o enriquecimento. `status` no vocabulário do schema (ok|error):
    `error` quando o Turso não respondeu — é o sinal de que os alertas estão saindo pelados.
    """
    global _enrichment_counter, _enrichment_histogram
    try:
        if _enrichment_counter is None:
            from opentelemetry import metrics as otel_metrics

            medidor = otel_metrics.get_meter("ops_centro.receiver.enrichment")
            _enrichment_counter = medidor.create_counter(
                "ops_centro_alert_enrichment_total",
                description="Enriquecimentos de alerta tentados, por desfecho (RF07)",
            )
            _enrichment_histogram = medidor.create_histogram(
                "ops_centro_alert_enrichment_duration_seconds",
                unit="s",
                description="Duração da consulta de contexto no Turso",
            )
        comuns = common_labels(APP_OPS_CENTRO)
        _enrichment_counter.add(1, {**comuns, "status": status})
        _enrichment_histogram.record(duracao, comuns)
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba o webhook
        logger.debug("falha ao medir o enriquecimento: %s", exc)


# --- consulta ---------------------------------------------------------------------------
def _timeout_from_env() -> float:
    try:
        return float(os.environ.get("ALERT_ENRICHMENT_TIMEOUT", "")) or DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _fetch(
    contexts: Sequence[AlertContext],
    connect_fn: Callable[[], Any],
    *,
    limit: int,
    minutes: int,
    now: datetime | None = None,
) -> list[tuple[list[LogLine], str]]:
    """Roda as consultas numa conexão só (bloqueante — vive numa thread).

    Conexão por chamada, e não uma global: o driver libsql é síncrono e uma consulta que
    estourou o deadline continua rodando na thread abandonada. Compartilhar conexão com
    ela é como o receiver ganharia um bug que só aparece sob incidente.
    """
    conn = connect_fn()
    try:
        return [
            related_logs(
                conn,
                trace_id=ctx.trace_id,
                app_name=ctx.app_name,
                tenant_id=ctx.tenant_id,
                limit=limit,
                minutes=minutes,
                now=now,
            )
            for ctx in contexts
        ]
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar a conexão do enriquecimento: %s", exc)


async def enrich_alerts(
    alerts: Sequence[HasAlertFields],
    *,
    connect_fn: Callable[[], Any] | None = None,
    timeout: float | None = None,
    limit: int = DEFAULT_LIMIT,
    minutes: int = DEFAULT_WINDOW_MINUTES,
    max_alerts: int = DEFAULT_MAX_ALERTS,
    now: datetime | None = None,
) -> list[EnrichedAlert]:
    """Enriquece os alertas de um webhook. **Nunca levanta.**

    Sem Turso configurado, com o Turso fora do ar ou estourado o deadline, devolve os
    mesmos alertas sem logs, com `reason` dizendo o porquê — quem monta a mensagem decide
    o que falar, e o alerta segue de qualquer jeito.
    """
    contexts = [AlertContext.from_alert(alerta) for alerta in alerts[:max_alerts]]
    if not contexts:
        return []

    if connect_fn is None:
        if not is_configured():
            return [
                EnrichedAlert(ctx, strategy=STRATEGY_DISABLED, reason="TURSO_DATABASE_URL ausente")
                for ctx in contexts
            ]
        connect_fn = connect

    inicio = time.perf_counter()
    try:
        resultados = await asyncio.wait_for(
            asyncio.to_thread(_fetch, contexts, connect_fn, limit=limit, minutes=minutes, now=now),
            timeout=timeout if timeout is not None else _timeout_from_env(),
        )
    except asyncio.TimeoutError:
        _record("error", time.perf_counter() - inicio)
        logger.warning("enriquecimento excedeu o deadline — alerta segue sem contexto")
        return [
            EnrichedAlert(ctx, strategy=STRATEGY_UNAVAILABLE, reason="timeout na consulta ao Turso")
            for ctx in contexts
        ]
    except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
        _record("error", time.perf_counter() - inicio)
        logger.warning("Turso indisponível para enriquecimento (%s) — alerta segue sem contexto", exc)
        return [
            EnrichedAlert(ctx, strategy=STRATEGY_UNAVAILABLE, reason=f"falha no Turso: {exc}")
            for ctx in contexts
        ]

    _record("ok", time.perf_counter() - inicio)
    return [
        EnrichedAlert(ctx, tuple(linhas), estrategia)
        for ctx, (linhas, estrategia) in zip(contexts, resultados)
    ]


def summarize(enriched: Sequence[EnrichedAlert]) -> dict[str, Any]:
    """Resumo do lote para o log estruturado do receiver (e para a resposta do webhook)."""
    return {
        "alerts": len(enriched),
        "enriched": sum(1 for item in enriched if item.enriched),
        "log_lines": sum(len(item.logs) for item in enriched),
        "strategies": sorted({item.strategy for item in enriched}),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
