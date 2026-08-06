"""Ação autônoma de baixo risco: pausar tool MCP com falha recorrente (RF09, issue #17).

Primeira ação automatizada da §8 do plano e o último critério de sucesso da §12 — "pelo
menos uma ação de baixo risco funcionando de ponta a ponta". O caminho inteiro:

```
alerta de erro por tool (#12) ─▶ receiver enriquece (#14)
        │
        ├─ decide (§1): N falhas da MESMA tool em X minutos, confirmadas no Turso
        ├─ executa: POST no admin_api do app  →  tool pausada por TTL
        ├─ audita: linha em `action_audit` (#19), com trace_id e gatilho
        ├─ avisa: mensagem no Telegram via Hermes (#15) dizendo o quê e por quê
        └─ despausa: no vencimento do TTL, e avisa de novo
```

Três travas, porque ação automática errada custa mais que alerta perdido:

1. **Kill switch** — `AUTONOMOUS_ACTIONS=off` desliga tudo, sem deploy;
2. **Evidência obrigatória** — sem contagem de falhas no Turso não há pausa. Alerta
   disparado é sintoma; a contagem é a prova. Turso fora do ar ⇒ nada acontece
   (falha fechada, ao contrário do enriquecimento, que falha aberto de propósito);
3. **Cooldown** — tool já pausada não é pausada de novo, senão cada avaliação da regra
   reiniciaria o TTL e a "pausa temporária" viraria permanente.

O TTL é dobrado de propósito: vai no corpo da chamada (o app despausa sozinho, mesmo que
o ops-centro suma) **e** fica em `expires_at` no audit (`sweep_expired` despausa e avisa,
mesmo que o app não tenha implementado expiração). Nenhum dos dois lados sozinho é
confiável o bastante para uma ação que ninguém está olhando.

    uv run python -m ops_centro.receiver.actions --status   # o que está pausado agora
    uv run python -m ops_centro.receiver.actions --sweep    # despausa o que venceu
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Sequence

import httpx

from ops_centro.conventions import APP_AGENTS_PLATFORM, APP_FILE_MEMORY, APP_OPS_CENTRO
from ops_centro.metrics import common_labels
from ops_centro.receiver.enrichment import EnrichedAlert
from ops_centro.receiver.hermes import (
    ACTION_EMOJI,
    KIND_ACTION,
    Notification,
    deliver,
    escape_md,
)
from ops_centro.turso.audit import (
    ACTION_PAUSE_TOOL,
    ACTION_RESUME_TOOL,
    ACTOR_AUTONOMOUS,
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_OK,
    ActionRecord,
    active_pause,
    expired_pauses,
    expires_at,
    record_action,
)
from ops_centro.turso.connection import connect, is_configured
from ops_centro.turso.log_reader import tool_failures

logger = logging.getLogger(__name__)

# Regra de decisão (§1 de docs/acoes-autonomas.md). 5 falhas em 30 min é o ponto de
# partida: acima do ruído de uma indisponibilidade momentânea do upstream e abaixo do
# volume de uma tool que está de fato quebrada.
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_WINDOW_MINUTES = 30

# 15 minutos de pausa: tempo de o upstream se recuperar sem que a tool fique fora de
# serviço a ponto de alguém precisar intervir no fim de semana.
DEFAULT_TTL_SECONDS = 900

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_QUERY_TIMEOUT_SECONDS = 2.0

# Chaves de label onde o nome da tool pode vir: `tool` é a label da métrica (catálogo da
# §7) e chega no alerta pelo `by (tool)` da regra; `tool_name` é o atributo do span.
TOOL_LABEL_KEYS = ("tool", "tool_name")

# Endpoint admin de cada app (cross-repo: a `admin_api` do mcp_brain é o lugar natural).
ADMIN_URL_ENV = {
    APP_AGENTS_PLATFORM: "ADMIN_API_AGENTS_PLATFORM_URL",
    APP_FILE_MEMORY: "ADMIN_API_FILE_MEMORY_URL",
}

# Motivos de bloqueio (viram `detail` no audit e texto na mensagem).
BLOCK_KILL_SWITCH = "kill switch ligado (AUTONOMOUS_ACTIONS=off)"
BLOCK_NO_ADMIN = "sem endpoint admin configurado para o app"
BLOCK_COOLDOWN = "tool já pausada"


def actions_enabled() -> bool:
    """Kill switch. Default **ligado**: quem não configurou endpoint admin já não age.

    `AUTONOMOUS_ACTIONS=off|0|false|no` desliga toda ação autônoma sem precisar de deploy —
    é a alavanca que se puxa às 3h da manhã quando o automatismo está atrapalhando.
    """
    return os.environ.get("AUTONOMOUS_ACTIONS", "on").strip().lower() not in (
        "off", "0", "false", "no", "nao", "não",
    )


def admin_url(app_name: str | None) -> str:
    variavel = ADMIN_URL_ENV.get(app_name or "")
    return os.environ.get(variavel, "").strip() if variavel else ""


def _admin_headers() -> dict[str, str]:
    """Mesmo padrão de token dos outros saltos (docs/alertas.md §3)."""
    token = os.environ.get("ADMIN_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}", "X-Admin-Token": token} if token else {}


def _int_env(nome: str, default: int) -> int:
    try:
        return int(os.environ.get(nome, "") or default)
    except ValueError:
        return default


# --- decisão --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Decision:
    """O que a regra decidiu sobre um alerta — e por quê (o `reason` vai no Telegram)."""

    act: bool
    tool: str = ""
    app_name: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    alertname: str = ""
    failures: int | None = None
    threshold: int = DEFAULT_FAILURE_THRESHOLD
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    reason: str = ""

    @property
    def triggered_by(self) -> str:
        """Gatilho, como fica no audit: alerta + evidência."""
        return f"{self.alertname or 'alerta'} · {self.failures} falha(s)/{self.window_minutes}min"

    def as_dict(self) -> dict[str, Any]:
        return {
            "act": self.act,
            "tool": self.tool,
            "app_name": self.app_name,
            "failures": self.failures,
            "threshold": self.threshold,
            "window_minutes": self.window_minutes,
            "reason": self.reason,
        }


def _tool_of(alert: EnrichedAlert) -> str:
    for chave in TOOL_LABEL_KEYS:
        valor = (alert.context.labels.get(chave) or "").strip()
        if valor:
            return valor
    return ""


def _count_failures(
    connect_fn: Callable[[], Any],
    tool: str,
    app_name: str | None,
    minutes: int,
    now: datetime | None,
) -> tuple[int, ActionRecord | None]:
    """Falhas da tool na janela + a pausa vigente, numa conexão só."""
    conn = connect_fn()
    try:
        return (
            tool_failures(conn, tool, app_name=app_name, minutes=minutes, now=now),
            active_pause(conn, tool, now),
        )
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar a conexão da decisão: %s", exc)


async def decide(
    alert: EnrichedAlert,
    *,
    connect_fn: Callable[[], Any] | None = None,
    threshold: int | None = None,
    window_minutes: int | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> Decision:
    """Decide se o alerta justifica pausar uma tool. **Nunca levanta.**

    Falha fechada em tudo que não for evidência clara: sem tool na label, alerta resolvido,
    Turso indisponível ou contagem abaixo do limiar ⇒ `act=False` com o motivo. Este é o
    inverso do enriquecimento (que segue sem contexto) porque as consequências são
    inversas: alerta pobre incomoda, pausa indevida tira uma tool do ar.
    """
    ctx = alert.context
    limiar = threshold if threshold is not None else _int_env(
        "AUTONOMOUS_PAUSE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD
    )
    janela = window_minutes if window_minutes is not None else _int_env(
        "AUTONOMOUS_PAUSE_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES
    )
    base = dict(
        app_name=ctx.app_name, tenant_id=ctx.tenant_id, trace_id=ctx.trace_id,
        alertname=ctx.alertname, threshold=limiar, window_minutes=janela,
    )

    tool = _tool_of(alert)
    if not tool:
        return Decision(False, reason="alerta sem label `tool`", **base)
    base["tool"] = tool
    if (ctx.status or "").lower() != "firing":
        return Decision(False, reason=f"alerta em `{ctx.status}` — nada a pausar", **base)

    if connect_fn is None:
        if not is_configured():
            return Decision(False, reason="sem Turso para confirmar as falhas", **base)
        connect_fn = connect
    try:
        falhas, pausa = await asyncio.wait_for(
            asyncio.to_thread(_count_failures, connect_fn, tool, ctx.app_name, janela, now),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — timeout ou driver levantando qualquer coisa
        logger.warning("não deu para confirmar as falhas de %s (%s) — sem ação", tool, exc)
        return Decision(False, reason=f"contagem de falhas indisponível ({exc})", **base)

    if pausa is not None:
        return Decision(
            False, failures=falhas, reason=f"{BLOCK_COOLDOWN} até {pausa.expires_at}", **base
        )
    if falhas < limiar:
        return Decision(
            False, failures=falhas,
            reason=f"{falhas} falha(s) em {janela}min — abaixo do limiar de {limiar}", **base,
        )
    return Decision(
        True, failures=falhas,
        reason=f"{falhas} falhas em {janela}min (limiar {limiar})", **base,
    )


# --- cliente do admin_api dos apps -------------------------------------------------------
async def admin_call(
    app_name: str | None,
    caminho: str,
    corpo: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Uma chamada ao endpoint admin do app. Devolve `(ok, detalhe)`, nunca levanta."""
    base = admin_url(app_name)
    if not base:
        return False, BLOCK_NO_ADMIN
    url = f"{base.rstrip('/')}{caminho}"
    proprio = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resposta = await http.post(url, json=corpo, headers=_admin_headers(), timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"erro de rede no admin_api: {exc}"
    finally:
        if proprio:
            await http.aclose()
    if resposta.status_code >= 300:
        return False, f"HTTP {resposta.status_code}: {resposta.text[:200]}"
    return True, f"HTTP {resposta.status_code}"


# --- métricas ----------------------------------------------------------------------------
_actions_counter: Any = None


def _record(status: str) -> None:
    """`ops_centro_autonomous_actions_total{status}` — quantas ações e com que desfecho."""
    global _actions_counter
    try:
        if _actions_counter is None:
            from opentelemetry import metrics as otel_metrics

            _actions_counter = otel_metrics.get_meter("ops_centro.receiver.actions").create_counter(
                "ops_centro_autonomous_actions_total",
                description="Ações autônomas do receiver, por desfecho (RF09, issue #17)",
            )
        _actions_counter.add(1, {**common_labels(APP_OPS_CENTRO), "status": status})
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba a ação
        logger.debug("falha ao contar a ação autônoma: %s", exc)


# --- auditoria (sempre, inclusive quando bloqueia) -----------------------------------------
async def write_audit(
    record: ActionRecord, *, connect_fn: Callable[[], Any] | None = None
) -> ActionRecord:
    """Grava a linha de auditoria. **Nunca levanta** — mas loga alto se não conseguir:
    ação sem trilha é exatamente o que a mitigação §10 existe para impedir."""
    if connect_fn is None:
        if not is_configured():
            logger.error("ação sem auditoria (Turso ausente): %s", record.as_dict())
            return record
        connect_fn = connect

    def _gravar() -> ActionRecord:
        conn = connect_fn()
        try:
            return record_action(conn, record)
        finally:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("erro ao fechar a conexão do audit: %s", exc)

    try:
        gravado = await asyncio.to_thread(_gravar)
    except Exception as exc:  # noqa: BLE001
        logger.error("falha ao auditar a ação (%s): %s", exc, record.as_dict())
        return record
    logger.info("ação registrada", extra={"action": record.action, "target": record.target,
                                          "status": record.status})
    return gravado


# --- notificação (transparência: toda ação vira mensagem) ----------------------------------
def _pause_message(record: ActionRecord, decision: Decision | None = None) -> str:
    minutos = int((record.ttl_seconds or 0) / 60)
    evidencia = (
        [escape_md(f"Evidência: {decision.failures} falhas no Turso.")]
        if decision is not None and decision.failures is not None
        else []
    )
    return "\n".join(
        [
            f"{ACTION_EMOJI} *{escape_md(f'Tool {record.target} pausada por {minutos} min')}*",
            escape_md(
                " · ".join(filter(None, (record.app_name, record.tenant_id, "ação autônoma")))
            ),
            "",
            escape_md(f"Motivo: {record.reason}"),
            *evidencia,
            escape_md(f"Gatilho: {record.triggered_by}"),
            escape_md(f"Despausa automática em {record.expires_at}."),
            escape_md("Desligue com AUTONOMOUS_ACTIONS=off; reverta agora com /despausar "
                      f"{record.target} (pede confirmação)."),
        ]
    )


def _resume_message(record: ActionRecord, ok: bool, detalhe: str) -> str:
    emoji, cabecalho = ("▶️", "Tool despausada") if ok else ("⚠️", "Falha ao despausar")
    return "\n".join(
        [
            f"{emoji} *{escape_md(f'{cabecalho}: {record.target}')}*",
            escape_md(" · ".join(filter(None, (record.app_name, "TTL vencido")))),
            "",
            escape_md(f"A pausa de {int((record.ttl_seconds or 0) / 60)} min terminou. {detalhe}"),
        ]
    )


async def _notify(record: ActionRecord, texto: str, **kwargs: Any) -> None:
    """Manda a ação para o Telegram pelo canal do #15 (transparência do critério de aceite)."""
    await deliver(
        Notification(
            kind=KIND_ACTION,
            status="executed" if record.status == STATUS_OK else record.status,
            severity="warning" if record.status != STATUS_OK else "info",
            title=f"{record.action} {record.target}",
            text=texto,
            app_name=record.app_name,
            tenant_id=record.tenant_id,
            trace_id=record.trace_id,
            alerts=(record.as_dict(),),
        ),
        **kwargs,
    )


# --- execução -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ActionResult:
    """Desfecho de uma ação: o que foi feito (ou por que não foi) e o registro gravado."""

    status: str
    action: str
    target: str
    detail: str = ""
    record: ActionRecord | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "expires_at": self.record.expires_at if self.record else None,
        }


async def pause_tool(
    decision: Decision,
    *,
    ttl_seconds: int | None = None,
    client: httpx.AsyncClient | None = None,
    connect_fn: Callable[[], Any] | None = None,
    notify: bool = True,
    now: datetime | None = None,
    notify_kwargs: dict[str, Any] | None = None,
) -> ActionResult:
    """Pausa a tool decidida, audita e avisa no Telegram. **Nunca levanta.**"""
    ttl = ttl_seconds if ttl_seconds is not None else _int_env(
        "AUTONOMOUS_PAUSE_TTL_SECONDS", DEFAULT_TTL_SECONDS
    )
    base = ActionRecord(
        action=ACTION_PAUSE_TOOL, target=decision.tool, status=STATUS_BLOCKED,
        actor=ACTOR_AUTONOMOUS, app_name=decision.app_name, tenant_id=decision.tenant_id,
        trace_id=decision.trace_id, triggered_by=decision.triggered_by, reason=decision.reason,
        ttl_seconds=ttl, expires_at=expires_at(ttl, now),
    )

    if not actions_enabled():
        registro = await write_audit(replace(base, detail=BLOCK_KILL_SWITCH), connect_fn=connect_fn)
        _record(STATUS_BLOCKED)
        logger.warning("ação autônoma bloqueada: %s", BLOCK_KILL_SWITCH)
        return ActionResult(STATUS_BLOCKED, ACTION_PAUSE_TOOL, decision.tool, BLOCK_KILL_SWITCH,
                            registro)

    ok, detalhe = await admin_call(
        decision.app_name,
        "/admin/tools/pause",
        {"tool_name": decision.tool, "ttl_seconds": ttl, "reason": decision.reason},
        client=client,
    )
    if not ok and detalhe == BLOCK_NO_ADMIN:
        registro = await write_audit(replace(base, detail=detalhe), connect_fn=connect_fn)
        _record(STATUS_BLOCKED)
        return ActionResult(STATUS_BLOCKED, ACTION_PAUSE_TOOL, decision.tool, detalhe, registro)

    status = STATUS_OK if ok else STATUS_ERROR
    registro = await write_audit(replace(base, status=status, detail=detalhe), connect_fn=connect_fn)
    _record(status)
    if notify:
        await _notify(registro, _pause_message(registro, decision), **(notify_kwargs or {}))
    return ActionResult(status, ACTION_PAUSE_TOOL, decision.tool, detalhe, registro)


async def resume_tool(
    pausa: ActionRecord,
    *,
    client: httpx.AsyncClient | None = None,
    connect_fn: Callable[[], Any] | None = None,
    notify: bool = True,
    notify_kwargs: dict[str, Any] | None = None,
) -> ActionResult:
    """Despausa uma tool cujo TTL venceu, audita e avisa. **Nunca levanta.**

    Registra no audit mesmo quando a chamada falha: sem a linha de `resume`, o sweep
    tentaria de novo para sempre — e, pior, ninguém saberia que a tool ficou pausada além
    do TTL prometido.
    """
    ok, detalhe = await admin_call(
        pausa.app_name, "/admin/tools/resume", {"tool_name": pausa.target}, client=client
    )
    registro = await write_audit(
        ActionRecord(
            action=ACTION_RESUME_TOOL, target=pausa.target,
            status=STATUS_OK if ok else STATUS_ERROR, actor=ACTOR_AUTONOMOUS,
            app_name=pausa.app_name, tenant_id=pausa.tenant_id, trace_id=pausa.trace_id,
            triggered_by=f"TTL vencido em {pausa.expires_at}",
            reason="despausa automática ao fim do TTL", ttl_seconds=pausa.ttl_seconds,
            detail=detalhe,
        ),
        connect_fn=connect_fn,
    )
    _record(STATUS_OK if ok else STATUS_ERROR)
    if notify:
        await _notify(registro, _resume_message(pausa, ok, detalhe), **(notify_kwargs or {}))
    return ActionResult(registro.status, ACTION_RESUME_TOOL, pausa.target, detalhe, registro)


def _pending(connect_fn: Callable[[], Any], now: datetime | None) -> list[ActionRecord]:
    conn = connect_fn()
    try:
        return expired_pauses(conn, now)
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar a conexão do sweep: %s", exc)


async def sweep_expired(
    *,
    connect_fn: Callable[[], Any] | None = None,
    client: httpx.AsyncClient | None = None,
    notify: bool = True,
    now: datetime | None = None,
    notify_kwargs: dict[str, Any] | None = None,
) -> list[ActionResult]:
    """Despausa tudo que venceu. **Nunca levanta.**

    Roda no start do receiver e depois de cada webhook: o estado das pausas mora no Turso
    (`expires_at`), então o despausar não depende de um `asyncio.Task` ter sobrevivido —
    um restart no meio da janela não deixa tool pausada para sempre.
    """
    if connect_fn is None:
        if not is_configured():
            return []
        connect_fn = connect
    try:
        pendentes = await asyncio.to_thread(_pending, connect_fn, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("não deu para listar as pausas vencidas: %s", exc)
        return []
    return [
        await resume_tool(
            pausa, client=client, connect_fn=connect_fn, notify=notify,
            notify_kwargs=notify_kwargs,
        )
        for pausa in pendentes
    ]


async def handle_alerts(
    enriched: Sequence[EnrichedAlert],
    *,
    connect_fn: Callable[[], Any] | None = None,
    client: httpx.AsyncClient | None = None,
    notify: bool = True,
    now: datetime | None = None,
    notify_kwargs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Avalia os alertas de um webhook e age no que justificar (o gancho do receiver).

    Uma tool por webhook no máximo — se o incidente derrubou três tools de uma vez, o
    problema quase certamente não é tool nenhuma, e pausar três é agravar o incidente.
    """
    if not actions_enabled():
        return []
    for alerta in enriched:
        decisao = await decide(alerta, connect_fn=connect_fn, now=now)
        if not decisao.act:
            if decisao.tool:
                logger.info("sem ação para a tool %s: %s", decisao.tool, decisao.reason)
            continue
        resultado = await pause_tool(
            decisao, client=client, connect_fn=connect_fn, notify=notify, now=now,
            notify_kwargs=notify_kwargs,
        )
        return [{**decisao.as_dict(), **resultado.as_dict()}]
    return []


# --- CLI --------------------------------------------------------------------------------------
def _print_state(connect_fn: Callable[[], Any]) -> int:
    from ops_centro.turso.audit import recent_actions

    conn = connect_fn()
    try:
        acoes = recent_actions(conn, limit=10)
        pendentes = expired_pauses(conn)
    finally:
        conn.close()
    print(f"kill switch: {'ligado (nada será executado)' if not actions_enabled() else 'desligado'}")
    print(f"pausas vencidas aguardando sweep: {len(pendentes)}")
    for acao in acoes:
        print(
            f"  {acao.created_at}  {acao.action:<12} {acao.target:<20} {acao.status:<10} "
            f"{acao.reason}"
        )
    if not acoes:
        print("  (nenhuma ação registrada)")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Ações autônomas do receiver (issue #17)")
    parser.add_argument("--status", action="store_true", help="últimas ações e pausas vencidas")
    parser.add_argument("--sweep", action="store_true", help="despausa o que venceu, agora")
    parser.add_argument("--json", action="store_true", help="saída JSON")
    args = parser.parse_args(argv)

    if not is_configured():
        print("erro: TURSO_DATABASE_URL não configurado (o audit das ações vive no Turso)")
        return 2

    if args.sweep:
        resultados = asyncio.run(sweep_expired())
        if args.json:
            print(json.dumps([r.as_dict() for r in resultados], ensure_ascii=False, indent=2))
        else:
            print(f"{len(resultados)} pausa(s) desfeita(s)")
            for resultado in resultados:
                print(f"  {resultado.target}: {resultado.status} ({resultado.detail})")
        return 0
    return _print_state(connect)


if __name__ == "__main__":
    sys.exit(main())
