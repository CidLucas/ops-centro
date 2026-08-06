"""Testes do writer de logs no Turso (RF05, issue #8).

Tudo roda contra um arquivo libsql local — sem rede, determinístico em CI.
"""

import json
import time

import libsql
import pytest

from ops_centro.turso import log_writer as lw
from ops_centro.turso.log_writer import TursoLogWriter, current_trace_id
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "logs.db")


@pytest.fixture
def migrated(db_path):
    conn = libsql.connect(database=db_path)
    apply_migrations(conn)
    conn.commit()
    return db_path


@pytest.fixture
def writer(migrated):
    w = TursoLogWriter(
        connect_fn=lambda: libsql.connect(database=migrated),
        batch_size=10,
        flush_interval=0.05,
    ).start()
    yield w
    w.close()


def rows(db_path):
    conn = libsql.connect(database=db_path)
    try:
        return conn.execute(
            "SELECT timestamp, app_name, tenant_id, trace_id, level, message, metadata "
            "FROM logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# --- migration ---------------------------------------------------------------
def test_migration_cria_tabela_e_e_idempotente(db_path):
    conn = libsql.connect(database=db_path)
    # A ordem é a do nome do arquivo: 0002 é o índice de suporte à retenção (issue #9),
    # 0003 o dead-letter do Hermes (#15), 0004 o audit de ações (#17/#19) e 0005 os tokens
    # de confirmação (#18).
    assert apply_migrations(conn) == [
        "0001_create_logs",
        "0002_logs_retention",
        "0003_hermes_dead_letter",
        "0004_actions_audit",
        "0005_action_confirmations",
    ]
    assert apply_migrations(conn) == []  # segunda passada não reaplica

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(logs)").fetchall()}
    assert {"idx_logs_trace_id", "idx_logs_app_time", "idx_logs_tenant_time"} <= indexes


# --- escrita -----------------------------------------------------------------
def test_grava_batch_com_todos_os_campos(writer, migrated):
    writer.log(
        "agents-platform",
        "error",
        "falha na tool de busca",
        tenant_id="acme",
        trace_id="0af7651916cd43dd8448eb211c80319c",
        metadata={"tool": "search", "retries": 2},
    )
    assert writer.flush()

    (row,) = rows(migrated)
    timestamp, app_name, tenant_id, trace_id, level, message, metadata = row
    assert (app_name, tenant_id, level, message) == (
        "agents-platform",
        "acme",
        "ERROR",
        "falha na tool de busca",
    )
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert json.loads(metadata) == {"tool": "search", "retries": 2}
    assert timestamp.endswith("+00:00")  # UTC explícito


def test_batch_por_tamanho_grava_sem_flush_explicito(writer, migrated):
    for i in range(10):  # batch_size do fixture
        writer.log("ops-centro", "INFO", f"evento {i}")

    deadline = time.monotonic() + 5
    while len(rows(migrated)) < 10 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(rows(migrated)) == 10
    assert writer.stats()["written"] == 10


def test_warn_e_normalizado_e_metadata_nao_serializavel_nao_derruba(writer, migrated):
    writer.log("ops-centro", "warn", "aviso", metadata={"obj": object()})
    assert writer.flush()

    (row,) = rows(migrated)
    assert row[4] == "WARNING"
    assert "object object" in row[6]


# --- correlação por trace_id --------------------------------------------------
def test_trace_id_vem_do_span_ativo(writer, migrated):
    trace = pytest.importorskip("opentelemetry.trace")
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("file_ingestion") as span:
        esperado = format(span.get_span_context().trace_id, "032x")
        assert current_trace_id() == esperado
        writer.log("file-memory-mcp", "INFO", "ingestão concluída", tenant_id="acme")
    assert writer.flush()

    (row,) = rows(migrated)
    assert row[3] == esperado
    assert len(row[3]) == 32  # mesmo formato exibido pelo Tempo


def test_sem_span_ativo_trace_id_fica_nulo(writer, migrated):
    writer.log("ops-centro", "INFO", "sem trace")
    assert writer.flush()
    assert rows(migrated)[0][3] is None


# --- RNF04: caminho quente ----------------------------------------------------
def test_log_nao_bloqueia_no_caminho_quente(migrated):
    """Com uma escrita artificialmente lenta (50ms/batch), 500 chamadas de log()
    precisam custar ordens de grandeza menos que as escritas que elas geram."""

    class ConexaoLenta:
        def executemany(self, sql, rows):
            time.sleep(0.05)

        def commit(self):
            pass

        def close(self):
            pass

    w = TursoLogWriter(connect_fn=ConexaoLenta, batch_size=5, flush_interval=0.01).start()
    try:
        inicio = time.perf_counter()
        for i in range(500):
            assert w.log("ops-centro", "INFO", f"quente {i}")
        decorrido = time.perf_counter() - inicio
    finally:
        w.close(timeout=0.1)

    assert decorrido < 0.5  # 500 escritas síncronas levariam ≥ 5s (100 batches × 50ms)


def test_fila_cheia_descarta_em_vez_de_bloquear():
    bloqueio = __import__("threading").Event()

    class ConexaoTravada:
        def executemany(self, sql, rows):
            bloqueio.wait(2)

        def commit(self):
            pass

        def close(self):
            pass

    w = TursoLogWriter(connect_fn=ConexaoTravada, batch_size=1, flush_interval=0.01, queue_size=5)
    w.start()
    try:
        resultados = [w.log("ops-centro", "INFO", f"m{i}") for i in range(200)]
        assert False in resultados  # descartou em vez de segurar o chamador
        assert w.stats()["dropped"] > 0
    finally:
        bloqueio.set()
        w.close(timeout=1)


# --- singleton / degradação graciosa ------------------------------------------
def test_log_to_turso_e_noop_sem_configuracao(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setattr(lw, "_writer", None)
    monkeypatch.setattr(lw, "_disabled_warned", False)

    assert lw.log_to_turso("ops-centro", None, None, "ERROR", "sem banco") is False
    assert lw.get_log_writer() is None


def test_log_to_turso_usa_writer_configurado(monkeypatch, migrated):
    monkeypatch.setattr(lw, "_writer", None)
    w = TursoLogWriter(connect_fn=lambda: libsql.connect(database=migrated), flush_interval=0.05)
    lw.configure_log_writer(w)
    try:
        assert lw.log_to_turso("file-memory-mcp", "acme", None, "ERROR", "boom", {"k": 1}) is True
        assert w.flush()
        (row,) = rows(migrated)
        assert (row[1], row[2], row[4]) == ("file-memory-mcp", "acme", "ERROR")
    finally:
        lw.shutdown_log_writer()
