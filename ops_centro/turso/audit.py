"""Trilha de auditoria das ações do Hermes (§10 do plano; tabela da migration 0004).

Toda ação — proposta, executada, bloqueada ou falha — vira uma linha em `action_audit`.
A issue #17 (pausa autônoma) é a primeira escritora e a #18 (ações com confirmação) usa a
mesma tabela; a #19 fecha o assunto com o comando `/acoes` e a política de retenção.

O que este módulo faz além de gravar é sustentar o **TTL da pausa**: `expires_at` mora no
banco, então despausar não depende de o processo continuar vivo. Um receiver que reinicia
no meio de uma pausa reconstrói o que está pendente com `expired_pauses()` — é a diferença
entre "TTL implementado" e "TTL confiável".

Como no `log_reader`, nenhuma função abre conexão: quem chama passa a conexão e controla o
deadline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Quem agiu. Ação autônoma é do próprio ops-centro; a #18 vai gravar `telegram:<user>`.
ACTOR_AUTONOMOUS = "ops-centro"

ACTION_PAUSE_TOOL = "pause_tool"
ACTION_RESUME_TOOL = "resume_tool"

# Desfecho, no mesmo vocabulário ok|error do schema, mais `bloqueado` — que não é falha:
# é o kill switch, o cooldown ou a falta de endpoint admin fazendo o seu trabalho.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_BLOCKED = "bloqueado"

COLUMNS = (
    "id, created_at, actor, action, target, app_name, tenant_id, trace_id, triggered_by, "
    "reason, ttl_seconds, expires_at, status, detail"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(momento: datetime) -> str:
    """Mesmo formato do writer de logs — comparação de timestamp como texto só é correta
    porque todo mundo grava UTC com a mesma precisão."""
    return momento.isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Uma linha de `action_audit`."""

    action: str
    target: str
    status: str
    actor: str = ACTOR_AUTONOMOUS
    app_name: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    triggered_by: str = ""
    reason: str = ""
    ttl_seconds: int | None = None
    expires_at: str | None = None
    detail: str = ""
    created_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> "ActionRecord":
        (
            ident, created_at, actor, action, target, app_name, tenant_id, trace_id,
            triggered_by, reason, ttl_seconds, expires_at, status, detail,
        ) = row
        return cls(
            id=ident, created_at=created_at, actor=actor, action=action, target=target,
            app_name=app_name, tenant_id=tenant_id, trace_id=trace_id,
            triggered_by=triggered_by or "", reason=reason or "",
            ttl_seconds=ttl_seconds, expires_at=expires_at, status=status, detail=detail or "",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "app_name": self.app_name,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "triggered_by": self.triggered_by,
            "reason": self.reason,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "status": self.status,
            "detail": self.detail,
        }


def expires_at(ttl_seconds: int, now: datetime | None = None) -> str:
    """Instante em que uma pausa com esse TTL vence."""
    return _iso((now or utcnow()) + timedelta(seconds=ttl_seconds))


def record_action(conn: Any, record: ActionRecord, now: datetime | None = None) -> ActionRecord:
    """Grava a ação e devolve o registro com `id`/`created_at` preenchidos."""
    criado = record.created_at or _iso(now or utcnow())
    cursor = conn.execute(
        "INSERT INTO action_audit "
        "(created_at, actor, action, target, app_name, tenant_id, trace_id, triggered_by, "
        " reason, ttl_seconds, expires_at, status, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            criado, record.actor, record.action, record.target, record.app_name,
            record.tenant_id, record.trace_id, record.triggered_by, record.reason,
            record.ttl_seconds, record.expires_at, record.status, record.detail,
        ),
    )
    conn.commit()
    return replace(record, created_at=criado, id=getattr(cursor, "lastrowid", None))


# `resume` posterior cancela a pausa: é o que distingue "pausa ativa" de "pausa que já
# foi desfeita". Sem esta cláusula, uma tool despausada à mão voltaria a contar como
# pausada e o sweep tentaria despausá-la de novo a cada rodada.
_SEM_RESUME_DEPOIS = (
    "NOT EXISTS (SELECT 1 FROM action_audit r WHERE r.action = ? AND r.target = p.target "
    "AND r.status = ? AND r.created_at > p.created_at)"
)


def active_pause(conn: Any, target: str, now: datetime | None = None) -> ActionRecord | None:
    """Pausa vigente de uma tool (ou `None`). É o cooldown: não se pausa o que já está
    pausado, senão cada avaliação da regra de alerta reiniciaria o TTL para sempre."""
    row = conn.execute(
        f"SELECT {COLUMNS} FROM action_audit p "
        "WHERE p.action = ? AND p.status = ? AND p.target = ? AND p.expires_at > ? "
        f"AND {_SEM_RESUME_DEPOIS} ORDER BY p.created_at DESC LIMIT 1",
        (
            ACTION_PAUSE_TOOL, STATUS_OK, target, _iso(now or utcnow()),
            ACTION_RESUME_TOOL, STATUS_OK,
        ),
    ).fetchone()
    return ActionRecord.from_row(row) if row else None


def expired_pauses(conn: Any, now: datetime | None = None, limit: int = 20) -> list[ActionRecord]:
    """Pausas cujo TTL venceu e que ainda não foram desfeitas — o trabalho do sweep."""
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM action_audit p "
        "WHERE p.action = ? AND p.status = ? AND p.expires_at IS NOT NULL AND p.expires_at <= ? "
        f"AND {_SEM_RESUME_DEPOIS} ORDER BY p.expires_at LIMIT ?",
        (
            ACTION_PAUSE_TOOL, STATUS_OK, _iso(now or utcnow()),
            ACTION_RESUME_TOOL, STATUS_OK, limit,
        ),
    ).fetchall()
    return [ActionRecord.from_row(row) for row in rows]


def recent_actions(conn: Any, limit: int = 10) -> list[ActionRecord]:
    """Últimas ações registradas — histórico do `/acoes` (issue #19)."""
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM action_audit ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [ActionRecord.from_row(row) for row in rows]
