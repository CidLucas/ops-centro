"""Ações de maior impacto com confirmação humana no Telegram (RF10, issue #18).

A #17 age sozinha porque pausar uma tool é reversível e de baixo risco. Reiniciar um
serviço ou desfazer uma pausa antes da hora não é: são as ações da §8 que exigem um humano
no meio, e esta é a mitigação direta do risco §10 ("ação automatizada com impacto
indevido").

```
/reiniciar agents-platform  ─▶ receiver propõe ─▶ Telegram mostra o impacto + 2 botões
                                     │                        │
                                     │              tocou em Confirmar
                                     ▼                        ▼
                          action_confirmations         POST /hermes/confirmacao
                          (token de uso único,                │
                           10 min de validade)      valida ─▶ executa ─▶ relata no thread
```

**Nada aqui executa sem os quatro sins:**

1. a ação está na allowlist `CONFIRMABLE` — não existe caminho para "execute este comando";
2. o chat está em `HERMES_ALLOWED_CHAT_IDS` — e **falha fechada**: allowlist vazia, ninguém
   confirma. Um bot de Telegram é um endpoint público de fato; a lista é o que separa "o
   canal do time" de "quem descobriu o bot";
3. o token existe, não venceu e **não foi usado** (a transição de estado é do banco, não
   deste processo — ver `turso/confirmations.py`);
4. quem confirma está no mesmo chat que propôs.

Qualquer não vira registro em `action_audit` com `status=bloqueado` e uma resposta dizendo
o motivo — recusa silenciosa é o pior dos dois mundos.

O kill switch `AUTONOMOUS_ACTIONS=off` **não** vale aqui, de propósito: ele desliga o que o
ops-centro faz sozinho. Uma pessoa confirmando um restart às 3h da manhã é exatamente o que
se quer que continue funcionando quando o automatismo foi desligado.

    uv run python -m ops_centro.receiver.confirmations --propose "restart_service agents-platform"
    uv run python -m ops_centro.receiver.confirmations --confirm <token>
    uv run python -m ops_centro.receiver.confirmations --pending
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

from ops_centro.conventions import APP_OPS_CENTRO
from ops_centro.metrics import common_labels
from ops_centro.receiver.actions import (
    ADMIN_URL_ENV,
    BLOCK_NO_ADMIN,
    admin_call,
    write_audit,
)
from ops_centro.receiver.hermes import escape_md

# O armazém dos tokens (`turso/confirmations.py`) tem o mesmo nome deste módulo — por isso
# entra com apelido em vez de nomes soltos, que se confundiriam com os daqui.
from ops_centro.turso import confirmations as store
from ops_centro.turso.audit import (
    ACTION_RESTART_SERVICE,
    ACTION_RESUME_TOOL,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PROPOSED,
    ActionRecord,
    active_pause,
    record_action,
    telegram_actor,
    utcnow,
)
from ops_centro.turso.connection import connect, is_configured

logger = logging.getLogger(__name__)

# Pseudo-ação usada só no audit de uma confirmação que não dá para atribuir a nada (token
# desconhecido, chat não autorizado). Não é executável — não está em CONFIRMABLE.
ACTION_CONFIRMATION = "confirmation"

# `callback_data` do botão inline. O Telegram limita a 64 bytes: prefixo + decisão + token
# de 22 caracteres cabem com folga.
CALLBACK_PREFIX = "ops"
DECISION_CONFIRM = "confirm"
DECISION_CANCEL = "cancel"

# Motivos de recusa (viram `detail` no audit e texto na resposta).
REJECT_NOT_CONFIRMABLE = "ação fora da allowlist de confirmáveis"
REJECT_UNAUTHORIZED = "chat não autorizado a propor/confirmar ações"
REJECT_UNKNOWN_TOKEN = "confirmação desconhecida"
REJECT_EXPIRED = "confirmação vencida"
REJECT_USED = "confirmação já usada"
REJECT_OTHER_CHAT = "confirmação vinda de outro chat"
REJECT_NO_DECISION = "botão não reconhecido"
REJECT_NO_TURSO = "sem Turso para registrar a confirmação"


# --- allowlist de ações ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConfirmableAction:
    """Uma ação que pode ser proposta — e nenhuma outra pode.

    `impact` é o texto que a pessoa lê **antes** de decidir. Escrevê-lo é metade do
    trabalho da issue: um botão "Confirmar" sem a frase que explica o estrago é só um
    caminho mais curto para o mesmo acidente.
    """

    name: str
    verb: str
    impact: str
    path: str
    build_body: Callable[[str, str], dict[str, Any]]
    targets: tuple[str, ...] = ()  # vazio = alvo livre (nome de tool)

    def label(self, target: str) -> str:
        return f"{self.verb} {target}"

    def accepts(self, target: str) -> bool:
        return bool(target) and (not self.targets or target in self.targets)


CONFIRMABLE: dict[str, ConfirmableAction] = {
    ACTION_RESTART_SERVICE: ConfirmableAction(
        name=ACTION_RESTART_SERVICE,
        verb="Reiniciar",
        impact=(
            "o processo cai e volta: requisições em voo se perdem e o app fica fora do ar "
            "por alguns segundos. Ingestão em andamento precisa ser refeita."
        ),
        path="/admin/service/restart",
        # Alvo fechado nos dois apps conhecidos: `target` vem do Telegram, e um alvo livre
        # aqui seria um caminho para reiniciar qualquer coisa que o admin_api aceitasse.
        targets=tuple(ADMIN_URL_ENV),
        build_body=lambda target, reason: {"app_name": target, "reason": reason},
    ),
    ACTION_RESUME_TOOL: ConfirmableAction(
        name=ACTION_RESUME_TOOL,
        verb="Despausar",
        impact=(
            "a tool volta a atender antes do fim do TTL da pausa — se a causa da falha não "
            "passou, os erros voltam junto e a pausa automática (#17) vai reagir de novo."
        ),
        path="/admin/tools/resume",
        build_body=lambda target, reason: {"tool_name": target, "reason": reason},
    ),
}


# --- allowlist de quem confirma ------------------------------------------------------------
def allowed_chats() -> frozenset[str]:
    """Chats autorizados (`HERMES_ALLOWED_CHAT_IDS=123,-100456`)."""
    bruto = os.environ.get("HERMES_ALLOWED_CHAT_IDS", "")
    return frozenset(parte.strip() for parte in bruto.split(",") if parte.strip())


def chat_authorized(chat_id: str | int | None) -> bool:
    """**Falha fechada**: sem allowlist configurada, ninguém propõe nem confirma nada.

    O contrário (lista vazia = todo mundo pode) transformaria um esquecimento de env var
    num bot público capaz de reiniciar produção.
    """
    permitidos = allowed_chats()
    return bool(permitidos) and str(chat_id or "").strip() in permitidos


def _ttl_seconds() -> int:
    padrao = store.DEFAULT_CONFIRMATION_TTL_SECONDS
    try:
        return int(os.environ.get("CONFIRMATION_TTL_SECONDS", "") or padrao)
    except ValueError:
        return padrao


# --- callback dos botões --------------------------------------------------------------------
def callback_data(decision: str, token: str) -> str:
    return f"{CALLBACK_PREFIX}:{decision}:{token}"


def parse_callback(bruto: str) -> tuple[str, str]:
    """`ops:confirm:<token>` → `("confirm", "<token>")`. Qualquer outra coisa → `("", "")`."""
    partes = (bruto or "").strip().split(":")
    if len(partes) != 3 or partes[0] != CALLBACK_PREFIX:
        return "", ""
    decisao, token = partes[1].lower(), partes[2]
    conhecida = decisao in (DECISION_CONFIRM, DECISION_CANCEL)
    return (decisao, token) if conhecida and token else ("", "")


def buttons_for(token: str) -> list[list[dict[str, str]]]:
    """Teclado inline no formato que o `sendMessage` do Telegram espera."""
    return [
        [
            {"text": "✅ Confirmar", "callback_data": callback_data(DECISION_CONFIRM, token)},
            {"text": "✖️ Cancelar", "callback_data": callback_data(DECISION_CANCEL, token)},
        ]
    ]


# --- métricas ---------------------------------------------------------------------------------
_counter: Any = None


def _record(status: str) -> None:
    """`ops_centro_confirmed_actions_total{status}` — propostas, execuções e recusas."""
    global _counter
    try:
        if _counter is None:
            from opentelemetry import metrics as otel_metrics

            _counter = otel_metrics.get_meter("ops_centro.receiver.confirmations").create_counter(
                "ops_centro_confirmed_actions_total",
                description="Ações com confirmação humana, por desfecho (RF10, issue #18)",
            )
        _counter.add(1, {**common_labels(APP_OPS_CENTRO), "status": status})
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba a ação
        logger.debug("falha ao contar a ação confirmada: %s", exc)


# --- infra de conexão --------------------------------------------------------------------------
def _resolve(connect_fn: Callable[[], Any] | None) -> Callable[[], Any] | None:
    if connect_fn is not None:
        return connect_fn
    return connect if is_configured() else None


async def _in_thread(connect_fn: Callable[[], Any], trabalho: Callable[[Any], Any]) -> Any:
    """Roda um bloco síncrono com conexão própria, fora do loop."""

    def _executar() -> Any:
        conn = connect_fn()
        try:
            return trabalho(conn)
        finally:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("erro ao fechar a conexão da confirmação: %s", exc)

    return await asyncio.to_thread(_executar)


# --- proposta -----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Proposal:
    """O que o Hermes precisa para mostrar a proposta (ou o motivo de não haver uma)."""

    ok: bool
    action: str = ""
    target: str = ""
    app_name: str | None = None
    token: str = ""
    expires_at: str = ""
    text: str = ""
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    detail: str = ""

    def as_response(self) -> dict[str, Any]:
        return {
            "command": self.action or "confirmacao",
            "text": self.text,
            "parse_mode": "MarkdownV2",
            "buttons": self.buttons,
            "data": {
                "proposed": self.ok,
                "action": self.action,
                "target": self.target,
                "app_name": self.app_name,
                "expires_at": self.expires_at,
                "detail": self.detail,
            },
        }


def _prazo(confirmacao: store.Confirmation) -> str:
    """"10 min" ou "45s" — arredondar um TTL curto para "1 min" seria mentir sobre quanto
    tempo a pessoa tem para decidir."""
    segundos = _iso_delta(confirmacao.created_at, confirmacao.expires_at)
    if segundos is None:
        return f"até {confirmacao.expires_at}"
    return f"{int(segundos // 60)} min" if segundos >= 60 else f"{int(segundos)}s"


def _proposal_text(spec: ConfirmableAction, target: str, confirmacao: store.Confirmation) -> str:
    return "\n".join(
        [
            f"🛑 *{escape_md('Confirmação necessária')}*",
            escape_md(spec.label(target)),
            "",
            escape_md(f"Impacto: {spec.impact}"),
            escape_md(f"Pedido por: {confirmacao.requested_by}"),
            escape_md(
                f"Vale por {_prazo(confirmacao)} (até {confirmacao.expires_at}) e só pode ser "
                "usada uma vez."
            ),
        ]
    )


def _iso_delta(inicio: str, fim: str) -> float | None:
    try:
        return (datetime.fromisoformat(fim) - datetime.fromisoformat(inicio)).total_seconds()
    except (TypeError, ValueError):
        return None


def _reject_text(motivo: str, sugestao: str = "") -> str:
    return "\n".join(
        filter(
            None,
            [
                f"🚫 *{escape_md('Ação recusada')}*",
                escape_md(f"Motivo: {motivo}."),
                escape_md(sugestao) if sugestao else "",
            ],
        )
    )


async def _audit_rejection(
    *,
    action: str,
    target: str,
    motivo: str,
    user: str,
    chat_id: str,
    connect_fn: Callable[[], Any] | None,
    app_name: str | None = None,
) -> None:
    """Recusa também é trilha (§10): quem tentou, o quê e por que não passou."""
    await write_audit(
        ActionRecord(
            action=action, target=target, status=STATUS_BLOCKED, actor=telegram_actor(user),
            app_name=app_name, triggered_by=f"Telegram (chat {chat_id or 'desconhecido'})",
            reason=motivo, detail=motivo,
        ),
        connect_fn=connect_fn,
    )
    _record(STATUS_BLOCKED)
    logger.warning("confirmação recusada (%s): %s/%s", motivo, action, target)


async def propose(
    action: str,
    target: str,
    *,
    user: str = "",
    chat_id: str = "",
    connect_fn: Callable[[], Any] | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> Proposal:
    """Propõe uma ação e devolve a mensagem com os botões. **Nunca levanta.**

    Nada é executado aqui: o retorno é só o convite. A execução mora em `confirm()`.
    """
    spec = CONFIRMABLE.get(action)
    if spec is None or not spec.accepts(target):
        motivo = (
            REJECT_NOT_CONFIRMABLE
            if spec is None
            else f"alvo inválido para {action} (aceita: {', '.join(spec.targets) or 'qualquer'})"
        )
        await _audit_rejection(
            action=action or ACTION_CONFIRMATION, target=target, motivo=motivo, user=user,
            chat_id=chat_id, connect_fn=_resolve(connect_fn),
        )
        return Proposal(False, action=action, target=target, detail=motivo,
                        text=_reject_text(motivo))

    if not chat_authorized(chat_id):
        await _audit_rejection(
            action=action, target=target, motivo=REJECT_UNAUTHORIZED, user=user,
            chat_id=chat_id, connect_fn=_resolve(connect_fn),
        )
        return Proposal(False, action=action, target=target, detail=REJECT_UNAUTHORIZED,
                        text=_reject_text(REJECT_UNAUTHORIZED))

    conectar = _resolve(connect_fn)
    if conectar is None:
        # Sem onde guardar o token de uso único não há confirmação confiável — e uma ação
        # de restart sem trilha é justamente o que a issue existe para evitar.
        logger.error("proposta recusada: %s", REJECT_NO_TURSO)
        return Proposal(False, action=action, target=target, detail=REJECT_NO_TURSO,
                        text=_reject_text(REJECT_NO_TURSO))

    ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
    token = store.new_token()
    momento = now or utcnow()

    def _gravar(conn: Any) -> tuple[store.Confirmation | None, str]:
        app_name = target if spec.name == ACTION_RESTART_SERVICE else None
        trace_id = None
        if spec.name == ACTION_RESUME_TOOL:
            pausa = active_pause(conn, target, momento)
            if pausa is None:
                return None, f"nenhuma pausa vigente para a tool {target}"
            app_name, trace_id = pausa.app_name, pausa.trace_id
        gravada = store.create(
            conn,
            store.Confirmation(
                action=spec.name, target=target, chat_id=str(chat_id),
                requested_by=telegram_actor(user), expires_at=store.expires_at(ttl, momento),
                app_name=app_name, trace_id=trace_id, reason=spec.impact,
            ),
            token,
            momento,
        )
        registro = record_action(
            conn,
            ActionRecord(
                action=spec.name, target=target, status=STATUS_PROPOSED,
                actor=telegram_actor(user), app_name=app_name, trace_id=trace_id,
                triggered_by=f"Telegram (chat {chat_id})", reason=spec.impact,
                ttl_seconds=ttl, expires_at=gravada.expires_at,
                detail="aguardando confirmação",
            ),
            momento,
        )
        store.link_audit(conn, token, registro.id)
        return gravada, ""

    try:
        gravada, erro = await _in_thread(conectar, _gravar)
    except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
        logger.error("falha ao registrar a proposta de %s %s: %s", action, target, exc)
        motivo = f"não consegui registrar a proposta ({exc})"
        return Proposal(False, action=action, target=target, detail=motivo,
                        text=_reject_text(motivo))

    if gravada is None:
        return Proposal(False, action=action, target=target, detail=erro,
                        text=_reject_text(erro))

    _record(STATUS_PROPOSED)
    logger.info("ação proposta", extra={"action": action, "target": target, "chat": chat_id})
    return Proposal(
        True, action=action, target=target, app_name=gravada.app_name, token=token,
        expires_at=gravada.expires_at, text=_proposal_text(spec, target, gravada),
        buttons=buttons_for(token),
    )


# --- confirmação e execução -----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Desfecho de um toque no botão."""

    status: str
    text: str
    action: str = ""
    target: str = ""
    app_name: str | None = None
    detail: str = ""
    executed: bool = False

    def as_response(self, message_id: int | None = None) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "parse_mode": "MarkdownV2",
            # O relato volta no mesmo thread da proposta (critério de aceite da issue).
            "reply_to_message_id": message_id,
            "data": {
                "action": self.action,
                "target": self.target,
                "app_name": self.app_name,
                "detail": self.detail,
                "executed": self.executed,
            },
        }


def _result_text(spec: ConfirmableAction, target: str, ok: bool, detalhe: str, quem: str) -> str:
    emoji, desfecho = ("✅", "executado") if ok else ("⚠️", "falhou")
    return "\n".join(
        [
            f"{emoji} *{escape_md(f'{spec.label(target)} — {desfecho}')}*",
            escape_md(f"Confirmado por {quem}."),
            "",
            escape_md(detalhe),
        ]
    )


def _fingerprint(token: str) -> str:
    """Identificador auditável de um token desconhecido — nunca o token em si."""
    return f"token:{store.token_hash(token)[:12]}" if token else "token:ausente"


async def confirm(
    *,
    token: str = "",
    callback: str = "",
    decision: str = "",
    user: str = "",
    chat_id: str = "",
    connect_fn: Callable[[], Any] | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> ConfirmationResult:
    """Valida a confirmação e executa a ação se tudo bater. **Nunca levanta.**

    A ordem das checagens é deliberada: a autorização do chat vem antes de qualquer
    consulta por token, para que um chat não autorizado não consiga nem sondar quais
    tokens existem.
    """
    if callback:
        decision, token = parse_callback(callback)
    decision = (decision or "").strip().lower()
    if decision not in (DECISION_CONFIRM, DECISION_CANCEL) or not token:
        await _audit_rejection(
            action=ACTION_CONFIRMATION, target=_fingerprint(token), motivo=REJECT_NO_DECISION,
            user=user, chat_id=chat_id, connect_fn=_resolve(connect_fn),
        )
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_NO_DECISION),
                                  detail=REJECT_NO_DECISION)

    if not chat_authorized(chat_id):
        await _audit_rejection(
            action=ACTION_CONFIRMATION, target=_fingerprint(token), motivo=REJECT_UNAUTHORIZED,
            user=user, chat_id=chat_id, connect_fn=_resolve(connect_fn),
        )
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_UNAUTHORIZED),
                                  detail=REJECT_UNAUTHORIZED)

    conectar = _resolve(connect_fn)
    if conectar is None:
        logger.error("confirmação recusada: %s", REJECT_NO_TURSO)
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_NO_TURSO),
                                  detail=REJECT_NO_TURSO)

    momento = now or utcnow()
    try:
        confirmacao = await _in_thread(conectar, lambda conn: store.find(conn, token))
    except Exception as exc:  # noqa: BLE001
        logger.error("falha ao ler a confirmação: %s", exc)
        motivo = f"não consegui validar a confirmação ({exc})"
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(motivo), detail=motivo)

    if confirmacao is None:
        await _audit_rejection(
            action=ACTION_CONFIRMATION, target=_fingerprint(token), motivo=REJECT_UNKNOWN_TOKEN,
            user=user, chat_id=chat_id, connect_fn=conectar,
        )
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_UNKNOWN_TOKEN),
                                  detail=REJECT_UNKNOWN_TOKEN)

    spec = CONFIRMABLE.get(confirmacao.action)
    contexto = dict(
        action=confirmacao.action, target=confirmacao.target, user=user, chat_id=chat_id,
        connect_fn=conectar, app_name=confirmacao.app_name,
    )

    if str(confirmacao.chat_id) != str(chat_id):
        await _audit_rejection(motivo=REJECT_OTHER_CHAT, **contexto)
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_OTHER_CHAT),
                                  action=confirmacao.action, target=confirmacao.target,
                                  detail=REJECT_OTHER_CHAT)

    if confirmacao.expired(momento):
        await _audit_rejection(motivo=REJECT_EXPIRED, **contexto)
        sugestao = f"Peça de novo com /{_command_for(confirmacao.action)} {confirmacao.target}."
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_EXPIRED, sugestao),
                                  action=confirmacao.action, target=confirmacao.target,
                                  detail=REJECT_EXPIRED)

    if spec is None:
        # Allowlist encolheu depois da proposta (deploy no meio do caminho). Não executa.
        await _audit_rejection(motivo=REJECT_NOT_CONFIRMABLE, **contexto)
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_NOT_CONFIRMABLE),
                                  action=confirmacao.action, target=confirmacao.target,
                                  detail=REJECT_NOT_CONFIRMABLE)

    quem = telegram_actor(user)
    novo_status = store.STATUS_CONFIRMED if decision == DECISION_CONFIRM else store.STATUS_CANCELLED
    try:
        venceu = await _in_thread(
            conectar,
            lambda conn: store.claim(conn, token, status=novo_status, decided_by=quem, now=momento),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("falha ao reservar a confirmação: %s", exc)
        motivo = f"não consegui reservar a confirmação ({exc})"
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(motivo),
                                  action=confirmacao.action, target=confirmacao.target,
                                  detail=motivo)

    # A reserva acontece **antes** da execução: se dois callbacks chegarem juntos (rede
    # ruim no celular reenvia), só um passa daqui — e o restart acontece uma vez.
    if not venceu:
        await _audit_rejection(motivo=REJECT_USED, **contexto)
        return ConfirmationResult(STATUS_BLOCKED, _reject_text(REJECT_USED),
                                  action=confirmacao.action, target=confirmacao.target,
                                  detail=REJECT_USED)

    if decision == DECISION_CANCEL:
        await write_audit(
            ActionRecord(
                action=confirmacao.action, target=confirmacao.target, status=STATUS_CANCELLED,
                actor=quem, app_name=confirmacao.app_name, trace_id=confirmacao.trace_id,
                triggered_by=f"Telegram (chat {chat_id})", reason="cancelado por quem propôs",
                detail="nada foi executado",
            ),
            connect_fn=conectar,
        )
        _record(STATUS_CANCELLED)
        return ConfirmationResult(
            STATUS_CANCELLED,
            "\n".join(
                [
                    f"✖️ *{escape_md(f'{spec.label(confirmacao.target)} — cancelado')}*",
                    escape_md("Nada foi executado."),
                ]
            ),
            action=confirmacao.action, target=confirmacao.target,
            app_name=confirmacao.app_name, detail="cancelado",
        )

    motivo_execucao = f"confirmado por {quem} no Telegram"
    ok, detalhe = await admin_call(
        confirmacao.app_name,
        spec.path,
        spec.build_body(confirmacao.target, motivo_execucao),
        client=client,
    )
    # Sem endpoint admin nada foi chamado: é `bloqueado` (config faltando), não `error`.
    status = STATUS_OK if ok else (STATUS_BLOCKED if detalhe == BLOCK_NO_ADMIN else STATUS_ERROR)
    await write_audit(
        ActionRecord(
            action=confirmacao.action, target=confirmacao.target, status=status, actor=quem,
            app_name=confirmacao.app_name, trace_id=confirmacao.trace_id,
            triggered_by=f"confirmação no Telegram (chat {chat_id})", reason=motivo_execucao,
            detail=detalhe,
        ),
        connect_fn=conectar,
    )
    _record(status)
    logger.info(
        "ação confirmada executada",
        extra={"action": confirmacao.action, "target": confirmacao.target, "status": status},
    )
    return ConfirmationResult(
        status, _result_text(spec, confirmacao.target, ok, detalhe, quem),
        action=confirmacao.action, target=confirmacao.target, app_name=confirmacao.app_name,
        detail=detalhe, executed=ok,
    )


def _command_for(action: str) -> str:
    """Comando do Telegram que propõe essa ação (para a dica na recusa)."""
    return {ACTION_RESTART_SERVICE: "reiniciar", ACTION_RESUME_TOOL: "despausar"}.get(
        action, "ajuda"
    )


# --- CLI ------------------------------------------------------------------------------------------
def _print_pending(connect_fn: Callable[[], Any]) -> int:
    conn = connect_fn()
    try:
        propostas = store.pending(conn)
    finally:
        conn.close()
    print(f"chats autorizados: {', '.join(sorted(allowed_chats())) or '(nenhum — falha fechada)'}")
    print(f"propostas pendentes: {len(propostas)}")
    for proposta in propostas:
        print(
            f"  {proposta.created_at}  {proposta.action:<16} {proposta.target:<20} "
            f"vence {proposta.expires_at}  ({proposta.requested_by})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Ações com confirmação humana (issue #18)")
    parser.add_argument(
        "--propose", metavar="'<ação> <alvo>'", help="ex: 'restart_service agents-platform'"
    )
    parser.add_argument("--confirm", metavar="TOKEN", help="confirma e executa")
    parser.add_argument("--cancel", metavar="TOKEN", help="cancela uma proposta pendente")
    parser.add_argument("--pending", action="store_true", help="propostas ainda válidas")
    parser.add_argument("--chat", default="", help="chat_id (default: o primeiro autorizado)")
    parser.add_argument("--user", default="cli", help="quem está pedindo")
    parser.add_argument("--json", action="store_true", help="saída JSON")
    args = parser.parse_args(argv)

    if not is_configured():
        print("erro: TURSO_DATABASE_URL não configurado (as confirmações vivem no Turso)")
        return 2
    chat = args.chat or next(iter(sorted(allowed_chats())), "")

    if args.propose:
        acao, _, alvo = args.propose.partition(" ")
        proposta = asyncio.run(propose(acao.strip(), alvo.strip(), user=args.user, chat_id=chat))
        if args.json:
            print(json.dumps({**proposta.as_response(), "token": proposta.token},
                             ensure_ascii=False, indent=2))
        else:
            print(proposta.text)
            if proposta.ok:
                print(f"\ntoken: {proposta.token}  (vence em {proposta.expires_at})")
        return 0 if proposta.ok else 1

    if args.confirm or args.cancel:
        resultado = asyncio.run(
            confirm(
                token=args.confirm or args.cancel,
                decision=DECISION_CONFIRM if args.confirm else DECISION_CANCEL,
                user=args.user, chat_id=chat,
            )
        )
        print(json.dumps(resultado.as_response(), ensure_ascii=False, indent=2)
              if args.json else resultado.text)
        return 0 if resultado.status in (STATUS_OK, STATUS_CANCELLED) else 1

    return _print_pending(connect)


if __name__ == "__main__":
    sys.exit(main())
