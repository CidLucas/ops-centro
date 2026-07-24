"""Consultas sob demanda vindas do Telegram (RF08, issue #16).

"Como estão os agentes hoje?" sem esperar alerta. O Hermes interpreta a mensagem do
Telegram, chama `POST /hermes/consulta` no receiver (mesmo padrão de token do webhook) e
repassa o `text` já formatado.

Comandos (§2 de docs/hermes.md):

    /status              saúde dos dois apps na última hora
    /status <tenant>     volume e erro de um tenant
    /erros [hoje|1h|30m] contagem e últimas linhas de erro no Turso
    /ajuda               esta lista

**Limite de custo é regra, não recomendação** (tarefa da issue): toda pergunta vira um
conjunto **fechado** de queries agregadas — três séries por app no Mimir e duas agregações
no Turso, todas com janela e `LIMIT`. Não existe caminho aqui para varredura livre de
traces no Tempo, que é o que estoura o free tier justamente no dia do incidente.

A resposta tem orçamento de tempo (`STATUS_QUERY_TIMEOUT`, default 8s, contra o critério
de aceite de <10s) e nunca levanta: parte que não respondeu vira "indisponível" na
mensagem, e o resto chega.

    uv run python -m ops_centro.receiver.status "/status"
    uv run python -m ops_centro.receiver.status "/erros hoje" --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ops_centro.conventions import (
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    ATTR_ENVIRONMENT,
    ATTR_TENANT_ID,
    ENV_DEV,
)
from ops_centro.receiver.hermes import escape_md
from ops_centro.turso.connection import connect, is_configured
from ops_centro.turso.log_reader import error_counts, recent_errors

logger = logging.getLogger(__name__)

CMD_STATUS = "status"
CMD_ERROS = "erros"
CMD_AJUDA = "ajuda"

DEFAULT_WINDOW = "1h"
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_ERROR_LINES = 5

# Apelidos de janela aceitos no `/erros` — "hoje" é como se pergunta, não "24h".
WINDOW_ALIASES = {"hoje": "24h", "agora": "15m", "semana": "7d"}

AJUDA = (
    "/status — saúde dos dois apps na última hora\n"
    "/status <tenant> — volume e erro de um tenant\n"
    "/erros [hoje|1h|30m] — contagem e últimas linhas de erro\n"
    "/ajuda — esta lista"
)


# --- comandos ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Command:
    """Um comando do Telegram já normalizado."""

    name: str
    argument: str = ""
    raw: str = ""

    @property
    def known(self) -> bool:
        return self.name in (CMD_STATUS, CMD_ERROS)


def parse_command(texto: str) -> Command:
    """Interpreta a mensagem do Telegram. Nada reconhecido vira `/ajuda`.

    Aceita a barra opcional e o sufixo de bot (`/status@hermes_bot`), que o Telegram
    acrescenta em grupo — sem isso o comando mais usado falharia só no celular de quem
    está em grupo, o cenário mais difícil de reproduzir depois.
    """
    bruto = (texto or "").strip()
    if not bruto:
        return Command(CMD_AJUDA, raw=bruto)
    partes = bruto.split()
    nome = partes[0].lstrip("/").split("@")[0].lower()
    argumento = " ".join(partes[1:]).strip()
    if nome in ("status", "saude", "saúde"):
        return Command(CMD_STATUS, argumento, bruto)
    if nome in ("erros", "erro", "errors"):
        return Command(CMD_ERROS, argumento, bruto)
    return Command(CMD_AJUDA, argumento, bruto)


def resolve_window(argumento: str, default: str = DEFAULT_WINDOW) -> str:
    """Janela pedida no comando ('hoje' → 24h; '30m' literal). Entrada estranha cai no
    default em vez de virar erro: quem digita no Telegram erra, e a resposta certa a um
    comando torto é a resposta padrão, não uma mensagem de sintaxe."""
    pedido = argumento.strip().lower()
    pedido = WINDOW_ALIASES.get(pedido, pedido)
    return pedido if re.fullmatch(r"\d{1,3}[smhd]", pedido or "") else default


# --- PromQL fechada ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AppQueries:
    """As séries que definem a saúde de um app — todas do catálogo da §7."""

    label: str
    counter: str  # counter com label `status` (ok|error)
    histogram: str  # histograma de latência do mesmo caminho
    tenant_counter: str  # counter por tenant (volume/uso)


HEALTH = {
    APP_AGENTS_PLATFORM: AppQueries(
        label="Agents Platform",
        counter="agents_platform_agent_executions_total",
        histogram="agents_platform_agent_execution_duration_seconds",
        tenant_counter="agents_platform_tenant_executions_total",
    ),
    APP_FILE_MEMORY: AppQueries(
        label="File Memory",
        counter="context_mcp_tool_calls_total",
        histogram="context_mcp_tool_call_duration_seconds",
        tenant_counter="context_mcp_tenant_files_total",
    ),
}


def _selector(metric: str, filtros: dict[str, str]) -> str:
    if not filtros:
        return metric
    corpo = ", ".join(f'{chave}="{valor}"' for chave, valor in sorted(filtros.items()))
    return f"{metric}{{{corpo}}}"


def _environment() -> str:
    return os.environ.get("ENVIRONMENT", ENV_DEV)


def build_queries(app: str, *, window: str = DEFAULT_WINDOW, tenant: str | None = None) -> dict[str, str]:
    """As (no máximo) três queries de um app. Nenhuma delas usa `by (...)`: a resposta é
    um número por pergunta, e menos séries lidas é menos custo no free tier.

    Com tenant, a fonte muda para o counter de volume por tenant — o único do catálogo
    que carrega `tenant_id` (a regra de cardinalidade de `conventions.py`), e por isso não
    há p95 por tenant: a latência é medida por agente/tool, não por cliente.
    """
    consultas = HEALTH[app]
    base = {ATTR_ENVIRONMENT: _environment()}
    if tenant:
        alvo = _selector(consultas.tenant_counter, {**base, ATTR_TENANT_ID: tenant})
        erro = _selector(consultas.tenant_counter, {**base, ATTR_TENANT_ID: tenant, "status": "error"})
        return {
            "volume": f"sum(increase({alvo}[{window}]))",
            "erro_pct": f"100 * sum(increase({erro}[{window}])) / "
            f"clamp_min(sum(increase({alvo}[{window}])), 1e-9)",
        }
    alvo = _selector(consultas.counter, base)
    erro = _selector(consultas.counter, {**base, "status": "error"})
    return {
        "volume": f"sum(increase({alvo}[{window}]))",
        "erro_pct": f"100 * sum(increase({erro}[{window}])) / "
        f"clamp_min(sum(increase({alvo}[{window}])), 1e-9)",
        "p95_s": f"histogram_quantile(0.95, sum by (le) "
        f"(rate({_selector(consultas.histogram + '_bucket', base)}[{window}])))",
    }


# --- coleta ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AppHealth:
    """Saúde de um app na janela consultada. `None` = a série não respondeu."""

    app: str
    label: str
    volume: float | None = None
    error_pct: float | None = None
    p95_seconds: float | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "volume": self.volume,
            "error_pct": self.error_pct,
            "p95_seconds": self.p95_seconds,
            "detail": self.detail,
        }

    def line(self) -> str:
        """Uma linha por app — o formato que cabe na tela do celular."""
        if self.volume is None and self.error_pct is None:
            return f"{self.label}: sem dados ({self.detail or 'nenhuma série na janela'})"
        pedacos = [f"{int(self.volume or 0)} execuções"]
        if self.error_pct is not None:
            pedacos.append(f"{self.error_pct:.1f}% erro")
        if self.p95_seconds is not None:
            pedacos.append(f"p95 {self.p95_seconds:.1f}s")
        marca = "🟢" if (self.error_pct or 0) < 5 else ("🟡" if (self.error_pct or 0) < 15 else "🔴")
        return f"{marca} {self.label}: " + " · ".join(pedacos)


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    """Erros no Turso na janela (contagem agregada + últimas linhas)."""

    counts: tuple[tuple[str, str, int], ...] = ()
    lines: tuple[str, ...] = ()
    detail: str = ""

    @property
    def total(self) -> int:
        return sum(total for _app, _level, total in self.counts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "counts": [
                {"app_name": app, "level": level, "total": total}
                for app, level, total in self.counts
            ],
            "lines": list(self.lines),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Resposta completa de um comando."""

    command: str
    window: str
    apps: tuple[AppHealth, ...] = ()
    errors: ErrorSummary | None = None
    tenant: str | None = None
    notes: tuple[str, ...] = ()
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "window": self.window,
            "tenant": self.tenant,
            "apps": [app.as_dict() for app in self.apps],
            "errors": self.errors.as_dict() if self.errors else None,
            "notes": list(self.notes),
            "generated_at": self.generated_at,
        }

    def as_markdown(self) -> str:
        """A resposta em MarkdownV2, pronta para o `sendMessage` do Hermes."""
        titulo = f"Status · últimas {self.window}" + (f" · tenant {self.tenant}" if self.tenant else "")
        partes = [f"📊 *{escape_md(titulo)}*"]
        partes += [escape_md(app.line()) for app in self.apps]
        if self.errors is not None:
            partes.append("")
            if self.errors.detail and not self.errors.counts:
                partes.append(escape_md(f"Erros: {self.errors.detail}"))
            else:
                resumo = ", ".join(
                    f"{app} {level.lower()} {total}"
                    for app, level, total in self.errors.counts
                ) or "nenhum"
                partes.append(escape_md(f"Erros no Turso ({self.errors.total}): {resumo}"))
                if self.errors.lines:
                    corpo = "\n".join(self.errors.lines).replace("`", "'")
                    partes.append("```\n" + corpo + "\n```")
        partes += [escape_md(nota) for nota in self.notes]
        return "\n".join(partes)


def _instant(cloud: Any, query: str) -> float | None:
    """Uma consulta instantânea no Prometheus do stack. `None` = não deu para responder."""
    from ops_centro.validation import _scalar

    if not cloud.prom.configured:
        return None
    try:
        resposta = cloud.prom.get("/api/v1/query", {"query": query})
    except Exception as exc:  # noqa: BLE001 — httpx levanta variantes; consulta não derruba
        logger.warning("consulta ao Prometheus falhou: %s", exc)
        return None
    if resposta.status_code != 200:
        logger.warning("Prometheus devolveu HTTP %s: %s", resposta.status_code, resposta.text[:150])
        return None
    try:
        return _scalar(resposta.json())
    except ValueError:
        return None


def collect_app_health(cloud: Any, app: str, *, window: str, tenant: str | None = None) -> AppHealth:
    """Saúde de um app (bloqueante — roda numa thread com deadline)."""
    consultas = HEALTH[app]
    if not cloud.prom.configured:
        return AppHealth(app, consultas.label, detail="Grafana não configurado (docs/secrets.md)")
    valores = {
        chave: _instant(cloud, query)
        for chave, query in build_queries(app, window=window, tenant=tenant).items()
    }
    return AppHealth(
        app=app,
        label=consultas.label,
        volume=valores.get("volume"),
        error_pct=valores.get("erro_pct"),
        p95_seconds=valores.get("p95_s"),
        detail="" if any(v is not None for v in valores.values()) else "Prometheus não respondeu",
    )


def collect_errors(
    connect_fn: Callable[[], Any], *, window: str, tenant: str | None = None
) -> ErrorSummary:
    """Erros do Turso na janela (bloqueante — roda na mesma thread da coleta)."""
    from ops_centro.validation import window_seconds

    minutos = max(window_seconds(window) // 60, 1)
    conn = connect_fn()
    try:
        contagens = tuple(error_counts(conn, minutes=minutos))
        linhas = tuple(
            linha.one_line()
            for linha in recent_errors(conn, limit=MAX_ERROR_LINES, minutes=minutos, tenant_id=tenant)
        )
        return ErrorSummary(contagens, linhas)
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar a conexão da consulta: %s", exc)


def _collect(
    command: Command,
    cloud: Any,
    connect_fn: Callable[[], Any] | None,
    *,
    window: str,
    apps: Sequence[str],
) -> StatusReport:
    """Executa a coleta inteira (bloqueante). Cada parte degrada sozinha."""
    tenant = command.argument.strip() or None if command.name == CMD_STATUS else None
    notas: list[str] = []

    saudes: tuple[AppHealth, ...] = ()
    if command.name == CMD_STATUS:
        saudes = tuple(collect_app_health(cloud, app, window=window, tenant=tenant) for app in apps)
        if tenant:
            notas.append("p95 não é medido por tenant (regra de cardinalidade do schema).")

    erros: ErrorSummary | None = None
    if command.name in (CMD_STATUS, CMD_ERROS):
        if connect_fn is None:
            erros = ErrorSummary(detail="Turso não configurado")
        else:
            try:
                erros = collect_errors(connect_fn, window=window, tenant=tenant)
            except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
                logger.warning("consulta de erros no Turso falhou: %s", exc)
                erros = ErrorSummary(detail=f"Turso indisponível: {exc}")

    return StatusReport(
        command=command.name, window=window, apps=saudes, errors=erros, tenant=tenant,
        notes=tuple(notas),
    )


async def run_command(
    texto: str,
    *,
    cloud: Any = None,
    connect_fn: Callable[[], Any] | None = None,
    timeout: float | None = None,
    apps: Sequence[str] = (APP_AGENTS_PLATFORM, APP_FILE_MEMORY),
) -> dict[str, Any]:
    """Interpreta e responde um comando. **Nunca levanta.**

    Devolve o dicionário que o Hermes consome: `text` em MarkdownV2 (pronto para o
    Telegram) e `data` estruturado (para ele decidir o que fazer de diferente).
    """
    comando = parse_command(texto)
    if comando.name == CMD_AJUDA:
        return {
            "command": CMD_AJUDA,
            "text": f"🤖 *{escape_md('Comandos disponíveis')}*\n" + escape_md(AJUDA),
            "parse_mode": "MarkdownV2",
            "data": {"commands": AJUDA.splitlines()},
        }

    janela = resolve_window(
        comando.argument if comando.name == CMD_ERROS else "",
        DEFAULT_WINDOW,
    )
    if cloud is None:
        from ops_centro.validation import GrafanaCloud

        cloud = GrafanaCloud.from_env()
    if connect_fn is None and is_configured():
        connect_fn = connect

    limite = timeout if timeout is not None else _timeout_from_env()
    try:
        relatorio = await asyncio.wait_for(
            asyncio.to_thread(_collect, comando, cloud, connect_fn, window=janela, apps=apps),
            timeout=limite,
        )
    except asyncio.TimeoutError:
        logger.warning("consulta '%s' excedeu %.1fs", comando.raw, limite)
        return {
            "command": comando.name,
            "text": escape_md(
                f"⏱ A consulta passou de {limite:.0f}s e foi cancelada. Tente uma janela menor."
            ),
            "parse_mode": "MarkdownV2",
            "data": {"error": "timeout", "window": janela},
        }
    except Exception as exc:  # noqa: BLE001 — consulta sob demanda nunca derruba o receiver
        logger.warning("consulta '%s' falhou: %s", comando.raw, exc)
        return {
            "command": comando.name,
            "text": escape_md(f"Não consegui responder agora: {exc}"),
            "parse_mode": "MarkdownV2",
            "data": {"error": str(exc)},
        }

    return {
        "command": comando.name,
        "text": relatorio.as_markdown(),
        "parse_mode": "MarkdownV2",
        "data": relatorio.as_dict(),
    }


def _timeout_from_env() -> float:
    try:
        return float(os.environ.get("STATUS_QUERY_TIMEOUT", "")) or DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Consultas sob demanda (RF08, issue #16)")
    parser.add_argument("comando", nargs="?", default="/status", help="ex: '/status acme'")
    parser.add_argument("--json", action="store_true", help="saída JSON (o que o Hermes recebe)")
    args = parser.parse_args(argv)

    resposta = asyncio.run(run_command(args.comando))
    if args.json:
        print(json.dumps(resposta, ensure_ascii=False, indent=2))
    else:
        print(resposta["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
