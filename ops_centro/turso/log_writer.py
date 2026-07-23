"""Writer de logs estruturados no Turso, correlacionados por trace_id (RF05).

Contrato de uso pelos apps:

    from ops_centro.turso import log_to_turso
    log_to_turso("agents-platform", tenant_id, None, "ERROR", "falha na tool X",
                 {"tool": "search"})

`trace_id=None` faz o writer pegar o trace do span OTel ativo — é o que garante que
o log gravado aqui aponte para o mesmo trace visível no Tempo (critério de aceite da
issue #8).

**Não bloqueia o caminho quente (RNF04):** `log_to_turso` só serializa a metadata e
faz `put_nowait` numa fila limitada; a escrita acontece numa thread daemon que agrupa
registros em batch (por tamanho ou por intervalo). Fila cheia = registro descartado e
contado em `stats()["dropped"]` — perder log é sempre preferível a segurar o request.
Thread (e não asyncio) porque o driver libsql é síncrono e os consumidores são mistos:
FastAPI async no agents-platform, worker síncrono no mcp_brain.

Sem `TURSO_DATABASE_URL` configurado, tudo vira no-op silencioso (um aviso único),
mesma degradação graciosa do `common/observability.py` do mcp_brain — CI determinístico
não precisa de banco.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ops_centro.turso.connection import connect, is_configured

logger = logging.getLogger(__name__)

INSERT_SQL = (
    "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL = 2.0
DEFAULT_QUEUE_SIZE = 10_000
MAX_MESSAGE_CHARS = 8_000

# Níveis canônicos. WARN é normalizado para WARNING (vocabulário do logging stdlib).
_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL", "ERR": "ERROR"}


@dataclass(frozen=True, slots=True)
class LogRecord:
    """Uma linha da tabela `logs` (db/migrations/0001_create_logs.sql)."""

    timestamp: str
    app_name: str
    tenant_id: str | None
    trace_id: str | None
    level: str
    message: str
    metadata: str | None

    def as_row(self) -> tuple[Any, ...]:
        return (
            self.timestamp,
            self.app_name,
            self.tenant_id,
            self.trace_id,
            self.level,
            self.message,
            self.metadata,
        )


class _FlushSignal:
    """Sentinela de flush síncrono — só usada em teste e no shutdown."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


def current_trace_id() -> str | None:
    """trace_id (32 hex, formato do Tempo) do span OTel ativo, ou None.

    Tolerante a ambiente sem OpenTelemetry instalado: correlação é um bônus, nunca
    um requisito para gravar o log.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def _normalize_level(level: str) -> str:
    upper = (level or "INFO").strip().upper()
    return _LEVEL_ALIASES.get(upper, upper)


def _serialize_metadata(metadata: Any) -> str | None:
    """Metadata vira JSON (coluna TEXT). Valor não serializável não derruba o log:
    cai para repr, porque um log truncado vale mais que uma exceção no caminho quente.
    """
    if metadata is None:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr(metadata)}, ensure_ascii=False)


class TursoLogWriter:
    """Fila + thread de escrita em batch na tabela `logs`.

    `connect_fn` é injetável para teste (arquivo local) — o default lê o env.
    A conexão é aberta preguiçosamente na thread, no primeiro batch: instanciar o
    writer nunca toca a rede.
    """

    def __init__(
        self,
        connect_fn: Callable[[], Any] | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._connect_fn = connect_fn or connect
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.05, flush_interval)
        self._queue: queue.Queue[LogRecord | _FlushSignal] = queue.Queue(maxsize=queue_size)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._lock = threading.Lock()
        self._dropped = 0
        self._written = 0
        self._failed = 0

    # -- ciclo de vida --------------------------------------------------------
    def start(self) -> "TursoLogWriter":
        """Sobe a thread de escrita (idempotente)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopping.clear()
                self._thread = threading.Thread(
                    target=self._run, name="turso-log-writer", daemon=True
                )
                self._thread.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        """Drena o que está na fila e encerra a thread. Seguro chamar mais de uma vez."""
        thread = self._thread
        if thread is None:
            return
        self._stopping.set()
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("writer de logs não encerrou em %.1fs — restam registros na fila", timeout)
        self._thread = None
        self._close_connection()

    def flush(self, timeout: float = 5.0) -> bool:
        """Bloqueia até os registros já enfileirados serem gravados. Uso: teste e shutdown."""
        if self._thread is None or not self._thread.is_alive():
            return False
        signal = _FlushSignal()
        try:
            self._queue.put(signal, timeout=timeout)
        except queue.Full:
            return False
        return signal.done.wait(timeout=timeout)

    # -- produção -------------------------------------------------------------
    def log(
        self,
        app_name: str,
        level: str,
        message: str,
        *,
        tenant_id: str | None = None,
        trace_id: str | None = None,
        metadata: Any = None,
        timestamp: str | None = None,
    ) -> bool:
        """Enfileira um log. Retorna False se foi descartado (fila cheia ou writer parado)."""
        record = LogRecord(
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            app_name=app_name,
            tenant_id=tenant_id or None,
            trace_id=trace_id or current_trace_id(),
            level=_normalize_level(level),
            message=(message or "")[:MAX_MESSAGE_CHARS],
            metadata=_serialize_metadata(metadata),
        )
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                logger.warning("fila do writer de logs cheia — %d registros descartados", self._dropped)
            return False
        return True

    def stats(self) -> dict[str, int]:
        """Contadores de operação (escritos, descartados por fila cheia, batches falhos)."""
        return {
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "queued": self._queue.qsize(),
        }

    # -- consumo (thread) -----------------------------------------------------
    def _run(self) -> None:
        pending: list[LogRecord] = []
        deadline = time.monotonic() + self._flush_interval
        pending_signals: list[_FlushSignal] = []

        while True:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout if not self._stopping.is_set() else 0.0)
            except queue.Empty:
                item = None

            if isinstance(item, _FlushSignal):
                pending_signals.append(item)
            elif item is not None:
                pending.append(item)

            should_flush = bool(pending) and (
                len(pending) >= self._batch_size
                or pending_signals
                or self._stopping.is_set()
                or time.monotonic() >= deadline
            )
            if should_flush:
                self._write_batch(pending)
                pending = []
                deadline = time.monotonic() + self._flush_interval
            elif not pending:
                deadline = time.monotonic() + self._flush_interval

            for signal in pending_signals:
                signal.done.set()
            pending_signals = []

            if self._stopping.is_set() and not pending and self._queue.empty():
                break

        self._close_connection()

    def _write_batch(self, batch: list[LogRecord]) -> None:
        """Insere um batch. Uma falha custa uma reconexão e uma segunda tentativa;
        persistindo o erro, o batch é descartado — a fila não pode crescer sem limite.
        """
        rows = [record.as_row() for record in batch]
        for attempt in (1, 2):
            conn = self._connection()
            if conn is None:
                self._failed += len(rows)
                return
            try:
                conn.executemany(INSERT_SQL, rows)
                conn.commit()
                self._written += len(rows)
                return
            except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
                self._close_connection()
                if attempt == 2:
                    self._failed += len(rows)
                    logger.warning("descartando %d logs após falha no Turso: %s", len(rows), exc)
                else:
                    logger.debug("falha ao gravar batch no Turso (%s) — reconectando", exc)

    def _connection(self) -> Any:
        if self._conn is None:
            try:
                self._conn = self._connect_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sem conexão com o Turso — logs não serão persistidos: %s", exc)
                return None
        return self._conn

    def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar conexão do writer: %s", exc)


# --- singleton de processo ----------------------------------------------------
_writer: TursoLogWriter | None = None
_writer_lock = threading.Lock()
_disabled_warned = False


def configure_log_writer(
    writer: TursoLogWriter | None = None, **kwargs: Any
) -> TursoLogWriter | None:
    """Configura (e sobe) o writer do processo. Idempotente.

    Sem `TURSO_DATABASE_URL` no ambiente e sem writer explícito, devolve None e o
    `log_to_turso` vira no-op — comportamento default em CI e dev sem banco.
    """
    global _writer, _disabled_warned
    with _writer_lock:
        if writer is not None:
            _writer = writer.start()
            return _writer
        if _writer is not None:
            return _writer
        if not is_configured():
            if not _disabled_warned:
                logger.info("TURSO_DATABASE_URL ausente — logs de longa retenção desativados")
                _disabled_warned = True
            return None
        _writer = TursoLogWriter(
            batch_size=int(os.environ.get("TURSO_LOG_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
            flush_interval=float(
                os.environ.get("TURSO_LOG_FLUSH_INTERVAL", DEFAULT_FLUSH_INTERVAL)
            ),
            **kwargs,
        ).start()
        return _writer


def get_log_writer() -> TursoLogWriter | None:
    """Writer do processo, se já configurado (não configura sozinho)."""
    return _writer


def shutdown_log_writer(timeout: float = 5.0) -> None:
    """Drena e encerra o writer do processo (chamar no shutdown do app)."""
    global _writer
    with _writer_lock:
        if _writer is not None:
            _writer.close(timeout=timeout)
            _writer = None


def log_to_turso(
    app_name: str,
    tenant_id: str | None,
    trace_id: str | None,
    level: str,
    message: str,
    metadata: Any = None,
) -> bool:
    """Assinatura pública consumida pelos apps (issue #8).

    Configura o writer na primeira chamada. Retorna False quando o log não foi
    enfileirado (writer desativado ou fila cheia) — nunca levanta.
    """
    writer = _writer or configure_log_writer()
    if writer is None:
        return False
    return writer.log(
        app_name,
        level,
        message,
        tenant_id=tenant_id,
        trace_id=trace_id,
        metadata=metadata,
    )
