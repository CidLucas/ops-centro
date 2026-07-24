"""Envio da notificação enriquecida ao Hermes → Telegram (RF07 — parte 2, issue #15).

Última perna do fluxo da Fase 3: Grafana → receiver (enriquecimento, #14) → **Hermes**
→ Telegram. Este módulo é o lado do receiver: monta o envelope, entrega com retry e
garante que uma queda do Hermes não apague o alerta.

**O contrato receiver→Hermes** (§1 de docs/hermes.md) é um JSON versionado com duas
camadas de propósito:

- `text` — a mensagem **já formatada** em MarkdownV2, com emoji de severidade e links
  clicáveis. O Hermes só precisa repassar ao `sendMessage`. Formatar aqui, e não lá, é o
  que torna a mensagem testável neste repo, onde vivem o schema e o enriquecimento;
- `alerts[]`, `severity`, `app_name`, ... — os mesmos dados **estruturados**, para o
  Hermes decidir roteamento, agrupar, ou reformatar quando quiser.

Três garantias, todas do critério de aceite da issue:

1. **retry com backoff** — falha de rede e 5xx/429 são tentadas de novo (`HERMES_RETRIES`);
2. **dead-letter** — esgotadas as tentativas, o envelope inteiro vai para
   `hermes_dead_letter` no Turso (migration 0003). Alerta não entregue fica registrado,
   nunca some em silêncio;
3. **rate limit** — um balde de fichas segura tempestade de alerta (`HERMES_RATE_LIMIT`
   por `HERMES_RATE_WINDOW`). O que foi suprimido é contado e anunciado na próxima
   mensagem que passar — silenciar sem dizer que silenciou seria pior que floodar.

Nada aqui levanta exceção para o chamador: o webhook do Grafana precisa responder 202
mesmo com o Telegram fora do ar.

    uv run python -m ops_centro.receiver.hermes --sample   # imprime o envelope de exemplo
    uv run python -m ops_centro.receiver.hermes --send     # manda o exemplo ao Hermes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx

from ops_centro import __version__
from ops_centro.conventions import APP_OPS_CENTRO
from ops_centro.metrics import common_labels
from ops_centro.receiver.enrichment import AlertContext, EnrichedAlert
from ops_centro.turso.connection import connect, is_configured
from ops_centro.turso.log_reader import LogLine

logger = logging.getLogger(__name__)

# Versão do contrato. O Hermes recusa (e loga) o que não souber ler — bump só quando o
# formato mudar de forma incompatível.
CONTRACT_VERSION = 1

KIND_ALERT = "alert"
KIND_ACTION = "action"

# Desfechos de uma entrega (o que a resposta do webhook e as métricas reportam).
DELIVERED = "entregue"
SUPPRESSED = "suprimido"
DISABLED = "desativado"
DEAD_LETTER = "dead-letter"

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 0.5

# Balde de fichas: 10 mensagens por 60s. Suficiente para um incidente real (o agrupamento
# da notification policy do #12 já junta o que é do mesmo alerta) e curto o bastante para
# que uma tempestade não vire 200 mensagens no celular.
DEFAULT_RATE_LIMIT = 10
DEFAULT_RATE_WINDOW_SECONDS = 60.0

# Emoji por severidade — o que dá para ler na notificação do celular sem abrir o app.
SEVERITY_EMOJI = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}
RESOLVED_EMOJI = "✅"
ACTION_EMOJI = "🤖"

# Linhas de log na mensagem. O Telegram corta em 4096 caracteres; 5 linhas de log com o
# resto do contexto ficam bem abaixo disso mesmo com mensagens longas.
MAX_LOG_LINES = 5
MAX_MESSAGE_CHARS = 3800

# Caracteres reservados do MarkdownV2 (spec da Bot API). Escapar de menos é mensagem que
# o Telegram recusa com 400 — o erro mais chato de descobrir em produção.
MARKDOWN_V2_RESERVED = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(texto: str) -> str:
    """Escapa um texto para MarkdownV2 (Bot API do Telegram)."""
    return "".join("\\" + ch if ch in MARKDOWN_V2_RESERVED else ch for ch in texto)


def _link(rotulo: str, url: str) -> str:
    """Link clicável no MarkdownV2. Dentro de `(...)`, só `)` e `\\` são escapados."""
    destino = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{escape_md(rotulo)}]({destino})"


# --- envelope ------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Notification:
    """Uma notificação pronta para o Hermes (o contrato da §1 de docs/hermes.md)."""

    kind: str
    status: str  # firing | resolved | executed (ações, #17)
    severity: str
    title: str
    text: str
    app_name: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    environment: str | None = None
    alerts: tuple[dict[str, Any], ...] = ()
    links: dict[str, str] = field(default_factory=dict)
    suppressed: int = 0

    @property
    def group_key(self) -> str:
        """Chave de agrupamento — a mesma dimensão da notification policy do #12."""
        return "/".join(
            parte or "-" for parte in (self.kind, self.title, self.app_name, self.tenant_id)
        )

    def as_payload(self) -> dict[str, Any]:
        """O JSON que vai no corpo do POST para o Hermes."""
        return {
            "version": CONTRACT_VERSION,
            "source": APP_OPS_CENTRO,
            "source_version": __version__,
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": self.kind,
            "status": self.status,
            "severity": self.severity,
            "group_key": self.group_key,
            "title": self.title,
            "text": self.text,
            "parse_mode": "MarkdownV2",
            "app_name": self.app_name,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "environment": self.environment,
            "links": self.links,
            "alerts": list(self.alerts),
            "suppressed": self.suppressed,
        }


def _emoji(status: str, severity: str) -> str:
    if status == "resolved":
        return RESOLVED_EMOJI
    return SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI["info"])


def render_markdown(enriched: Sequence[EnrichedAlert]) -> str:
    """Mensagem do Telegram em MarkdownV2 a partir dos alertas enriquecidos.

    A ordem responde as perguntas de quem foi acordado, na ordem em que se pergunta: o
    que quebrou (título com emoji de severidade), quem sentiu (app/tenant/ambiente), o
    que apareceu no log e para onde ir (links). O formato longo fica no dashboard; aqui
    entra só o que se lê no celular.
    """
    if not enriched:
        return ""
    principal = enriched[0].context
    partes: list[str] = []

    cabecalho = _emoji(principal.status, principal.severity)
    titulo = principal.summary or principal.alertname or "alerta sem título"
    partes.append(f"{cabecalho} *{escape_md(titulo)}*")

    alvo = " · ".join(
        filter(None, (principal.app_name, principal.tenant_id, principal.environment))
    )
    estado = (principal.status or "").upper()
    rodape_estado = f"{estado}/{principal.severity}" if principal.severity else estado
    partes.append(escape_md(f"{rodape_estado}{' — ' + alvo if alvo else ''}"))

    if principal.description:
        partes.append("")
        partes.append(escape_md(principal.description.strip()))

    primeiro = enriched[0]
    if primeiro.logs:
        origem = (
            f"trace {principal.trace_id}"
            if primeiro.strategy == "trace"
            else f"janela recente de {principal.app_name}"
        )
        partes.append("")
        partes.append(escape_md(f"Últimos logs ({origem}):"))
        # Bloco de código: o log não passa pelo parser de Markdown, então mensagem de
        # erro com `_` ou `*` não quebra o envio nem some da tela.
        corpo = "\n".join(linha.one_line() for linha in primeiro.logs[:MAX_LOG_LINES])
        partes.append("```\n" + corpo.replace("\\", "\\\\").replace("`", "'") + "\n```")
    elif primeiro.reason:
        partes.append("")
        partes.append(escape_md(f"Sem logs correlacionados ({primeiro.reason})."))

    links = primeiro.links()
    if links:
        rotulos = {"trace": "trace no Tempo", "dashboard": "dashboard", "runbook": "runbook"}
        partes.append("")
        partes.append(" · ".join(_link(rotulos.get(k, k), v) for k, v in links.items()))

    if len(enriched) > 1:
        partes.append("")
        partes.append(escape_md(f"+{len(enriched) - 1} alerta(s) no mesmo disparo."))

    texto = "\n".join(partes)
    return texto if len(texto) <= MAX_MESSAGE_CHARS else texto[:MAX_MESSAGE_CHARS] + "…"


def notification_from_alerts(enriched: Sequence[EnrichedAlert]) -> Notification | None:
    """Envelope de um webhook de alerta. `None` quando não há nada a notificar."""
    if not enriched:
        return None
    principal = enriched[0]
    ctx = principal.context
    return Notification(
        kind=KIND_ALERT,
        status=ctx.status,
        severity=ctx.severity,
        title=ctx.summary or ctx.alertname or "alerta sem título",
        text=render_markdown(enriched),
        app_name=ctx.app_name,
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        environment=ctx.environment,
        alerts=tuple(item.as_dict() for item in enriched),
        links=principal.links(),
    )


# --- rate limit ----------------------------------------------------------------------
class RateLimiter:
    """Balde de fichas simples: `capacity` mensagens por `window` segundos.

    Estado em processo, e não no Turso, de propósito: o receiver é um container só (o
    deploy da #13), e um limitador que precisa de ida ao banco para decidir se manda uma
    mensagem seria mais uma coisa a falhar durante o incidente.
    """

    def __init__(self, capacity: int, window: float) -> None:
        self.capacity = max(capacity, 1)
        self.window = max(window, 1.0)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self.suppressed = 0

    def _refill(self, agora: float) -> None:
        taxa = self.capacity / self.window
        self._tokens = min(self.capacity, self._tokens + (agora - self._updated) * taxa)
        self._updated = agora

    def take(self) -> tuple[bool, int]:
        """Tenta consumir uma ficha. Devolve `(passou, suprimidas_desde_a_última)`."""
        self._refill(time.monotonic())
        if self._tokens < 1:
            self.suppressed += 1
            return False, self.suppressed
        self._tokens -= 1
        pendentes, self.suppressed = self.suppressed, 0
        return True, pendentes


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """Limitador do processo (criado na primeira notificação, lendo o env)."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(
            _int_env("HERMES_RATE_LIMIT", DEFAULT_RATE_LIMIT),
            _float_env("HERMES_RATE_WINDOW", DEFAULT_RATE_WINDOW_SECONDS),
        )
    return _limiter


def reset_limiter() -> None:
    """Zera o limitador (usado nos testes e pelo `--send` da CLI)."""
    global _limiter
    _limiter = None


# --- env ------------------------------------------------------------------------------
def _int_env(nome: str, default: int) -> int:
    try:
        return int(os.environ.get(nome, "") or default)
    except ValueError:
        return default


def _float_env(nome: str, default: float) -> float:
    try:
        return float(os.environ.get(nome, "") or default)
    except ValueError:
        return default


def webhook_url() -> str:
    return os.environ.get("HERMES_WEBHOOK_URL", "").strip()


def _auth_headers() -> dict[str, str]:
    """Token nos dois formatos, mesmo padrão do Grafana→receiver (docs/alertas.md §3)."""
    token = os.environ.get("HERMES_WEBHOOK_TOKEN", "").strip()
    if not token:
        return {}
    return {"X-Hermes-Token": token, "Authorization": f"Bearer {token}"}


# --- métricas do próprio envio ---------------------------------------------------------
_notify_counter: Any = None
_notify_histogram: Any = None


def _record(status: str, duracao: float) -> None:
    """Conta e cronometra a entrega. `status` no vocabulário ok|error do schema — o resto
    (suprimido, desativado) vira label do contador de descartes, não desfecho de entrega.
    """
    global _notify_counter, _notify_histogram
    try:
        if _notify_counter is None:
            from opentelemetry import metrics as otel_metrics

            medidor = otel_metrics.get_meter("ops_centro.receiver.hermes")
            _notify_counter = medidor.create_counter(
                "ops_centro_hermes_notifications_total",
                description="Notificações enviadas ao Hermes, por desfecho (RF07, issue #15)",
            )
            _notify_histogram = medidor.create_histogram(
                "ops_centro_hermes_delivery_duration_seconds",
                unit="s",
                description="Duração da entrega da notificação ao Hermes",
            )
        comuns = common_labels(APP_OPS_CENTRO)
        _notify_counter.add(1, {**comuns, "status": status})
        _notify_histogram.record(duracao, comuns)
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba o webhook
        logger.debug("falha ao medir a entrega ao Hermes: %s", exc)


# --- dead-letter -----------------------------------------------------------------------
def _insert_dead_letter(conn: Any, notification: Notification, reason: str, attempts: int) -> None:
    conn.execute(
        "INSERT INTO hermes_dead_letter "
        "(created_at, kind, group_key, severity, app_name, tenant_id, trace_id, attempts, "
        " reason, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            notification.kind,
            notification.group_key,
            notification.severity,
            notification.app_name,
            notification.tenant_id,
            notification.trace_id,
            attempts,
            reason[:500],
            json.dumps(notification.as_payload(), ensure_ascii=False),
        ),
    )
    conn.commit()


async def dead_letter(
    notification: Notification,
    reason: str,
    attempts: int,
    *,
    connect_fn: Any = None,
) -> bool:
    """Registra uma notificação não entregue. **Nunca levanta.**

    Se o Turso também estiver fora, o envelope sai no log em nível ERROR: pior que não
    entregar é não haver rastro nenhum de que existiu um alerta.
    """
    if connect_fn is None:
        if not is_configured():
            logger.error(
                "notificação perdida (sem Turso para dead-letter): %s",
                json.dumps(notification.as_payload(), ensure_ascii=False),
            )
            return False
        connect_fn = connect

    def _gravar() -> None:
        conn = connect_fn()
        try:
            _insert_dead_letter(conn, notification, reason, attempts)
        finally:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("erro ao fechar a conexão do dead-letter: %s", exc)

    try:
        await asyncio.to_thread(_gravar)
    except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
        logger.error(
            "falha ao gravar o dead-letter (%s); notificação perdida: %s",
            exc,
            json.dumps(notification.as_payload(), ensure_ascii=False),
        )
        return False
    logger.warning("notificação em dead-letter após %s tentativa(s): %s", attempts, reason)
    return True


# --- entrega ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Desfecho de uma tentativa de notificar o Hermes."""

    status: str
    attempts: int = 0
    detail: str = ""
    suppressed: int = 0
    dead_lettered: bool = False

    @property
    def delivered(self) -> bool:
        return self.status == DELIVERED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "detail": self.detail,
            "suppressed": self.suppressed,
            "dead_lettered": self.dead_lettered,
        }


def _retryable(status_code: int) -> bool:
    """429 e 5xx voltam; 4xx não — token errado não melhora na terceira tentativa."""
    return status_code == 429 or status_code >= 500


async def deliver(
    notification: Notification,
    *,
    client: httpx.AsyncClient | None = None,
    url: str | None = None,
    retries: int | None = None,
    backoff: float | None = None,
    timeout: float | None = None,
    connect_fn: Any = None,
    sleep=asyncio.sleep,
) -> DeliveryResult:
    """Entrega a notificação ao Hermes, com retry e dead-letter. **Nunca levanta.**

    Sem `HERMES_WEBHOOK_URL` a função vira no-op declarado (`desativado`) — é o estado de
    quem ainda não plugou o Hermes, e não pode custar um 500 no webhook do Grafana.
    """
    destino = (url if url is not None else webhook_url()).strip()
    if not destino:
        logger.info("HERMES_WEBHOOK_URL ausente — notificação não enviada")
        return DeliveryResult(DISABLED, detail="HERMES_WEBHOOK_URL não configurado")

    passou, suprimidas = get_limiter().take()
    if not passou:
        logger.warning("rate limit do Hermes: %s notificação(ões) suprimida(s)", suprimidas)
        return DeliveryResult(SUPPRESSED, suppressed=suprimidas, detail="rate limit")

    # As suprimidas desde a última entrega são anunciadas nesta mensagem: silenciar sem
    # dizer que silenciou é como se perde a confiança no canal.
    if suprimidas:
        notification = replace(
            notification,
            suppressed=suprimidas,
            text=notification.text
            + "\n"
            + escape_md(f"+{suprimidas} notificação(ões) suprimida(s) por rate limit."),
        )

    tentativas = retries if retries is not None else _int_env("HERMES_RETRIES", DEFAULT_RETRIES)
    espera = backoff if backoff is not None else _float_env("HERMES_BACKOFF", DEFAULT_BACKOFF_SECONDS)
    limite = timeout if timeout is not None else _float_env("HERMES_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    payload = notification.as_payload()
    headers = _auth_headers()

    inicio = time.perf_counter()
    proprio = client is None
    http = client or httpx.AsyncClient(timeout=limite)
    motivo = ""
    try:
        for tentativa in range(1, max(tentativas, 1) + 1):
            try:
                resposta = await http.post(destino, json=payload, headers=headers, timeout=limite)
            except httpx.HTTPError as exc:
                motivo = f"erro de rede: {exc}"
            else:
                if resposta.status_code < 300:
                    _record("ok", time.perf_counter() - inicio)
                    logger.info(
                        "notificação entregue ao Hermes",
                        extra={"tentativas": tentativa, "group_key": notification.group_key},
                    )
                    return DeliveryResult(
                        DELIVERED, attempts=tentativa, suppressed=suprimidas,
                        detail=f"HTTP {resposta.status_code}",
                    )
                motivo = f"HTTP {resposta.status_code}: {resposta.text[:200]}"
                if not _retryable(resposta.status_code):
                    break
            if tentativa < max(tentativas, 1):
                # Jitter para dois receivers (ou dois alertas simultâneos) não baterem
                # no Hermes que acabou de voltar exatamente no mesmo instante.
                await sleep(espera * (2 ** (tentativa - 1)) * (1 + random.random() * 0.25))
    finally:
        if proprio:
            await http.aclose()

    _record("error", time.perf_counter() - inicio)
    gravou = await dead_letter(notification, motivo, tentativas, connect_fn=connect_fn)
    return DeliveryResult(
        DEAD_LETTER, attempts=tentativas, detail=motivo, suppressed=suprimidas,
        dead_lettered=gravou,
    )


async def notify_alerts(
    enriched: Sequence[EnrichedAlert], **kwargs: Any
) -> DeliveryResult:
    """Monta e entrega a notificação de um webhook de alerta (o caminho do receiver)."""
    notificacao = notification_from_alerts(enriched)
    if notificacao is None:
        return DeliveryResult(DISABLED, detail="nada a notificar")
    return await deliver(notificacao, **kwargs)


# --- CLI (teste ponta a ponta) -------------------------------------------------------------
def _exemplo() -> Notification:
    """Notificação sintética — a mesma forma de um alerta real, com dados de mentira.

    É o que o teste ponta a ponta do #15 usa para provar o caminho receiver→Hermes→Telegram
    sem depender de a métrica de verdade ultrapassar um limiar.
    """
    ctx = AlertContext(
        alertname="Agents Platform: taxa de erro por tool MCP acima de 10%",
        status="firing",
        severity="warning",
        app_name="agents-platform",
        tenant_id="acme",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        environment=os.environ.get("ENVIRONMENT", "dev"),
        summary="Tool search falhando em 23.4% das chamadas",
        description="Teste sintético do canal receiver→Hermes (issue #15).",
        runbook_url="https://github.com/CidLucas/ops-centro/blob/main/docs/alertas.md",
    )
    logs = (
        LogLine(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            app_name="agents-platform",
            tenant_id="acme",
            trace_id=ctx.trace_id,
            level="ERROR",
            message="tool search: upstream timeout (3/3 tentativas)",
            metadata='{"tool": "search"}',
        ),
    )
    enriquecido = EnrichedAlert(ctx, logs, strategy="trace")
    return Notification(
        kind=KIND_ALERT,
        status=ctx.status,
        severity=ctx.severity,
        title=ctx.summary,
        text=render_markdown([enriquecido]),
        app_name=ctx.app_name,
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        environment=ctx.environment,
        alerts=(enriquecido.as_dict(),),
        links=enriquecido.links(),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Canal receiver→Hermes (issue #15)")
    parser.add_argument("--sample", action="store_true", help="imprime o envelope de exemplo")
    parser.add_argument("--send", action="store_true", help="envia o exemplo ao HERMES_WEBHOOK_URL")
    args = parser.parse_args(argv)

    exemplo = _exemplo()
    if args.send:
        if not webhook_url():
            print("erro: HERMES_WEBHOOK_URL não configurado (ver .env.example)")
            return 2
        resultado = asyncio.run(deliver(exemplo))
        print(json.dumps(resultado.as_dict(), ensure_ascii=False, indent=2))
        return 0 if resultado.delivered else 1

    print(json.dumps(exemplo.as_payload(), ensure_ascii=False, indent=2))
    if not args.sample:
        print("\n--- mensagem renderizada (MarkdownV2) ---")
        print(exemplo.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
