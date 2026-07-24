"""Leitura dos logs de longa retenção para enriquecer alertas (RF07, issue #14).

O lado de leitura do writer da issue #8: o mesmo `trace_id` que o app gravou junto do log
é o que o alerta traz nas labels (issue #12), e é por ele que o contexto é recuperado.

Dois caminhos, nesta ordem:

1. **por `trace_id`** — os logs daquela execução exata, o contexto mais preciso possível
   (índice `idx_logs_trace_id`, migration 0001);
2. **por janela `app_name` [+ `tenant_id`]** — quando o alerta não carrega trace (o caso
   comum: alerta nasce de uma série agregada, não de uma execução). Aqui só os níveis que
   valem investigação — ERROR/CRITICAL/WARNING — porque INFO em janela larga devolveria
   ruído e gastaria row reads do free tier (índices `idx_logs_app_time`/`idx_logs_tenant_time`).

As mesmas tabelas respondem mais duas perguntas de fases seguintes, sempre por agregação no
banco (nunca trazendo linhas para contar em Python): "quantos erros hoje?" (`error_counts`
/ `recent_errors`, consultas sob demanda do RF08, issue #16) e "esta tool falhou quantas
vezes na janela?" (`tool_failures`, evidência da pausa autônoma do RF09, issue #17).

**Nenhuma função aqui abre conexão sozinha.** Quem chama passa a conexão, e o receiver a
abre e fecha dentro do timeout do enriquecimento — consulta de contexto nunca pode segurar
a entrega do alerta (critério de aceite do #14).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Níveis que interessam numa investigação de alerta. INFO/DEBUG ficam de fora do caminho
# de janela: em volume eles afogam a linha que importa.
INVESTIGATION_LEVELS = ("CRITICAL", "ERROR", "WARNING")

DEFAULT_LIMIT = 20
DEFAULT_WINDOW_MINUTES = 30
MAX_MESSAGE_CHARS = 500

COLUMNS = "timestamp, app_name, tenant_id, trace_id, level, message, metadata"


@dataclass(frozen=True, slots=True)
class LogLine:
    """Uma linha da tabela `logs` como o enriquecimento a devolve."""

    timestamp: str
    app_name: str
    tenant_id: str | None
    trace_id: str | None
    level: str
    message: str
    metadata: str | None

    @classmethod
    def from_row(cls, row: Any) -> "LogLine":
        timestamp, app_name, tenant_id, trace_id, level, message, metadata = row
        return cls(
            timestamp=timestamp,
            app_name=app_name,
            tenant_id=tenant_id,
            trace_id=trace_id,
            level=level,
            # Truncar aqui, e não na origem: mensagem de 8 mil caracteres não cabe no
            # Telegram e não ajuda quem lê o alerta no celular.
            message=(message or "")[:MAX_MESSAGE_CHARS],
            metadata=metadata,
        )

    def metadata_dict(self) -> dict[str, Any]:
        """Metadata como dicionário (vazio se não for JSON de objeto)."""
        if not self.metadata:
            return {}
        try:
            valor = json.loads(self.metadata)
        except (TypeError, ValueError):
            return {}
        return valor if isinstance(valor, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "app_name": self.app_name,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "level": self.level,
            "message": self.message,
            "metadata": self.metadata_dict() or None,
        }

    def one_line(self) -> str:
        """Uma linha legível — é o que vai virar corpo da mensagem no Telegram (#15)."""
        tenant = f" [{self.tenant_id}]" if self.tenant_id else ""
        return f"{self.timestamp} {self.level}{tenant} {self.message}"


def _window_start(minutes: int, now: datetime | None = None) -> str:
    """Início da janela no mesmo formato que o writer grava (issue #8).

    Comparar timestamp como texto só é correto porque os dois lados usam
    `isoformat(timespec="milliseconds")` em UTC — mesmo comprimento, mesmo offset.
    """
    agora = now or datetime.now(timezone.utc)
    return (agora - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


def logs_by_trace(conn: Any, trace_id: str, limit: int = DEFAULT_LIMIT) -> list[LogLine]:
    """Últimos `limit` logs de um trace, do mais recente para o mais antigo."""
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM logs WHERE trace_id = ? ORDER BY timestamp DESC LIMIT ?",
        (trace_id, limit),
    ).fetchall()
    return [LogLine.from_row(row) for row in rows]


def logs_by_window(
    conn: Any,
    app_name: str,
    *,
    tenant_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    minutes: int = DEFAULT_WINDOW_MINUTES,
    levels: tuple[str, ...] = INVESTIGATION_LEVELS,
    now: datetime | None = None,
) -> list[LogLine]:
    """Logs relevantes de um app (opcionalmente de um tenant) na janela recente."""
    where = ["app_name = ?", "timestamp >= ?"]
    params: list[Any] = [app_name, _window_start(minutes, now)]
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    if levels:
        where.append(f"level IN ({', '.join('?' for _ in levels)})")
        params.extend(levels)
    params.append(limit)

    rows = conn.execute(
        f"SELECT {COLUMNS} FROM logs WHERE {' AND '.join(where)} "
        "ORDER BY timestamp DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    return [LogLine.from_row(row) for row in rows]


def error_counts(
    conn: Any,
    *,
    minutes: int = 60,
    now: datetime | None = None,
    levels: tuple[str, ...] = ("CRITICAL", "ERROR"),
) -> list[tuple[str, str, int]]:
    """Contagem de erros por (app, nível) na janela — `(app_name, level, total)`.

    Agregação no banco, não em Python: a consulta sob demanda do #16 precisa devolver um
    número, e trazer as linhas para contá-las aqui gastaria row reads do free tier
    proporcionais ao incidente — exatamente quando não se quer gastar.
    """
    marcadores = ", ".join("?" for _ in levels)
    rows = conn.execute(
        f"SELECT app_name, level, COUNT(*) FROM logs "
        f"WHERE timestamp >= ? AND level IN ({marcadores}) "
        "GROUP BY app_name, level ORDER BY COUNT(*) DESC",
        (_window_start(minutes, now), *levels),
    ).fetchall()
    return [(row[0], row[1], int(row[2])) for row in rows]


def recent_errors(
    conn: Any,
    *,
    limit: int = 5,
    minutes: int = 60,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> list[LogLine]:
    """Últimos erros da janela, de qualquer app (opcionalmente de um tenant)."""
    where = ["timestamp >= ?", "level IN ('CRITICAL', 'ERROR')"]
    params: list[Any] = [_window_start(minutes, now)]
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    params.append(limit)
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM logs WHERE {' AND '.join(where)} "
        "ORDER BY timestamp DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    return [LogLine.from_row(row) for row in rows]


def tool_failures(
    conn: Any,
    tool: str,
    *,
    app_name: str | None = None,
    minutes: int = 30,
    now: datetime | None = None,
) -> int:
    """Quantas falhas da tool `tool` foram logadas na janela (evidência da pausa, #17).

    O nome da tool vem do `metadata` JSON que o app grava junto do log — aceita as duas
    chaves em uso (`tool`, do vocabulário de labels de métrica, e `tool_name`, do
    vocabulário de atributos de span; ver `ops_centro/conventions.py`).
    """
    where = [
        "timestamp >= ?",
        "level IN ('CRITICAL', 'ERROR')",
        "COALESCE(json_extract(metadata, '$.tool'), json_extract(metadata, '$.tool_name')) = ?",
    ]
    params: list[Any] = [_window_start(minutes, now), tool]
    if app_name:
        where.append("app_name = ?")
        params.append(app_name)
    row = conn.execute(
        f"SELECT COUNT(*) FROM logs WHERE {' AND '.join(where)}", tuple(params)
    ).fetchone()
    return int(row[0]) if row else 0


def related_logs(
    conn: Any,
    *,
    trace_id: str | None,
    app_name: str | None,
    tenant_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    minutes: int = DEFAULT_WINDOW_MINUTES,
    now: datetime | None = None,
) -> tuple[list[LogLine], str]:
    """Logs relacionados ao alerta + a estratégia que os achou (`trace` | `janela` | `nenhuma`).

    A estratégia volta junto porque muda a leitura do resultado: dez linhas de um trace são
    a execução que falhou; dez linhas de uma janela são uma amostra do que estava errado ao
    redor. Dizer qual é qual na mensagem evita conclusão apressada.
    """
    if trace_id:
        linhas = logs_by_trace(conn, trace_id, limit=limit)
        if linhas:
            return linhas, "trace"
        # Trace sem log gravado é normal (o app pode não ter logado nada naquela execução):
        # cair para a janela é melhor que devolver alerta pelado.
        logger.debug("trace_id %s sem logs no Turso — caindo para a janela", trace_id)
    if app_name:
        return (
            logs_by_window(conn, app_name, tenant_id=tenant_id, limit=limit, minutes=minutes,
                           now=now),
            "janela",
        )
    return [], "nenhuma"
