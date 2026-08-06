"""Tokens de confirmação de uso único das ações de maior impacto (RF10, issue #18).

O `audit.py` guarda o que aconteceu; este módulo guarda o que **ainda pode** acontecer.
Uma proposta vira uma linha `pendente` em `action_confirmations` (migration 0005) com
prazo de validade, e só sai de lá uma vez:

    propose  ─▶ pendente ──confirm──▶ confirmado ─▶ executa
                    │
                    ├──cancel───────▶ cancelado
                    └──venceu───────▶ expirado

Duas decisões que valem o comentário:

1. **Só o hash do token vai para o banco.** Quem tiver acesso de leitura (um backup, um
   dump, um log de query) não consegue confirmar nada em nome de ninguém. O token em claro
   existe por 10 minutos dentro do `callback_data` do botão e mais nada;
2. **A troca de status é um UPDATE condicional** (`WHERE status = 'pendente'`), não um
   read-modify-write. O Telegram reenvia callback quando a rede do celular oscila, e é o
   banco — não a ordem em que os dois pedidos chegaram — que decide qual dos dois executa.

Como no `log_reader` e no `audit`, nenhuma função abre conexão: quem chama passa a conexão
e controla o deadline.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ops_centro.turso.audit import iso, utcnow

logger = logging.getLogger(__name__)

# 10 minutos (a tarefa da issue). Curto o bastante para que um "sim" não sobreviva à troca
# de turno, longo o bastante para quem foi olhar o dashboard antes de decidir.
DEFAULT_CONFIRMATION_TTL_SECONDS = 600

STATUS_PENDING = "pendente"
STATUS_CONFIRMED = "confirmado"
STATUS_CANCELLED = "cancelado"
STATUS_EXPIRED = "expirado"

COLUMNS = (
    "id, token_hash, created_at, expires_at, action, target, app_name, tenant_id, trace_id, "
    "chat_id, requested_by, reason, status, decided_at, decided_by, audit_id"
)


def new_token() -> str:
    """Token de confirmação. 16 bytes urlsafe = 22 caracteres — cabe com folga no limite
    de 64 bytes do `callback_data` do Telegram, junto com o prefixo e a decisão."""
    return secrets.token_urlsafe(16)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Uma proposta aguardando (ou já tendo recebido) decisão humana."""

    action: str
    target: str
    chat_id: str
    requested_by: str
    expires_at: str
    token_hash: str = ""
    app_name: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    reason: str = ""
    status: str = STATUS_PENDING
    created_at: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    audit_id: int | None = None
    id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Confirmation":
        (
            ident, hash_, created_at, expires_at, action, target, app_name, tenant_id,
            trace_id, chat_id, requested_by, reason, status, decided_at, decided_by, audit_id,
        ) = row
        return cls(
            id=ident, token_hash=hash_, created_at=created_at, expires_at=expires_at,
            action=action, target=target, app_name=app_name, tenant_id=tenant_id,
            trace_id=trace_id, chat_id=chat_id, requested_by=requested_by, reason=reason or "",
            status=status, decided_at=decided_at, decided_by=decided_by, audit_id=audit_id,
        )

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= iso(now or utcnow())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "action": self.action,
            "target": self.target,
            "app_name": self.app_name,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "chat_id": self.chat_id,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "status": self.status,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "audit_id": self.audit_id,
        }


def expires_at(ttl_seconds: int, now: datetime | None = None) -> str:
    """Instante em que um token com esse TTL deixa de valer."""
    return iso((now or utcnow()) + timedelta(seconds=ttl_seconds))


def create(
    conn: Any,
    confirmation: Confirmation,
    token: str,
    now: datetime | None = None,
) -> Confirmation:
    """Grava a proposta pendente. O token em claro **não** entra no banco."""
    criado = confirmation.created_at or iso(now or utcnow())
    hash_ = token_hash(token)
    cursor = conn.execute(
        "INSERT INTO action_confirmations "
        "(token_hash, created_at, expires_at, action, target, app_name, tenant_id, trace_id, "
        " chat_id, requested_by, reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hash_, criado, confirmation.expires_at, confirmation.action, confirmation.target,
            confirmation.app_name, confirmation.tenant_id, confirmation.trace_id,
            confirmation.chat_id, confirmation.requested_by, confirmation.reason,
            STATUS_PENDING,
        ),
    )
    conn.commit()
    return replace(
        confirmation, token_hash=hash_, created_at=criado, status=STATUS_PENDING,
        id=getattr(cursor, "lastrowid", None),
    )


def find(conn: Any, token: str) -> Confirmation | None:
    """Proposta correspondente ao token (ou `None` se não existe)."""
    row = conn.execute(
        f"SELECT {COLUMNS} FROM action_confirmations WHERE token_hash = ?", (token_hash(token),)
    ).fetchone()
    return Confirmation.from_row(row) if row else None


def claim(
    conn: Any,
    token: str,
    *,
    status: str,
    decided_by: str,
    now: datetime | None = None,
) -> bool:
    """Tenta tirar a proposta de `pendente`. Devolve se **esta** chamada conseguiu.

    É aqui que mora o "uso único": o `WHERE status = ?` faz o banco escolher o vencedor
    quando o mesmo callback chega duas vezes. Driver sem `rowcount` confiável cai no
    caminho conservador — reler o status e comparar — em vez de assumir sucesso.
    """
    agora = iso(now or utcnow())
    cursor = conn.execute(
        "UPDATE action_confirmations SET status = ?, decided_at = ?, decided_by = ? "
        "WHERE token_hash = ? AND status = ?",
        (status, agora, decided_by, token_hash(token), STATUS_PENDING),
    )
    conn.commit()
    afetadas = getattr(cursor, "rowcount", -1)
    if afetadas is not None and afetadas >= 0:
        return afetadas == 1
    atual = find(conn, token)
    return bool(atual and atual.status == status and atual.decided_by == decided_by)


def link_audit(conn: Any, token: str, audit_id: int | None) -> None:
    """Amarra a proposta à sua linha em `action_audit` (#19). Falha aqui não é fatal:
    a auditoria já existe; o que se perde é o atalho de navegação entre as duas tabelas."""
    if audit_id is None:
        return
    conn.execute(
        "UPDATE action_confirmations SET audit_id = ? WHERE token_hash = ?",
        (audit_id, token_hash(token)),
    )
    conn.commit()


def pending(conn: Any, now: datetime | None = None, limit: int = 20) -> list[Confirmation]:
    """Propostas ainda válidas — o que aparece no `--pending` da CLI."""
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM action_confirmations WHERE status = ? AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT ?",
        (STATUS_PENDING, iso(now or utcnow()), limit),
    ).fetchall()
    return [Confirmation.from_row(row) for row in rows]


def expire_stale(conn: Any, now: datetime | None = None) -> int:
    """Marca como `expirado` o que venceu sem decisão. Cosmético — a checagem de validade
    é feita na hora de confirmar, não por este job — mas mantém o `--pending` honesto."""
    cursor = conn.execute(
        "UPDATE action_confirmations SET status = ? WHERE status = ? AND expires_at <= ?",
        (STATUS_EXPIRED, STATUS_PENDING, iso(now or utcnow())),
    )
    conn.commit()
    afetadas = getattr(cursor, "rowcount", -1)
    return max(afetadas, 0) if afetadas is not None else 0
