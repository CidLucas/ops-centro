"""Testes da retenção/limpeza dos logs no Turso (issue #9).

Tudo contra um arquivo libsql local, com `now` injetado — sem rede e sem depender de
relógio de parede, do mesmo jeito que os testes do writer (#8).
"""

from datetime import datetime, timedelta, timezone

import libsql
import pytest

from ops_centro.turso import retention as r
from ops_centro.turso.migrate import apply_migrations
from ops_centro.turso.retention import RetentionPolicy, purge

pytestmark = pytest.mark.unit

AGORA = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)

# (nível, idade em dias) → o que deve sobrar está anotado em cada teste.
AMOSTRA = [
    ("ERROR", 100),  # fora da janela de 90d
    ("ERROR", 10),
    ("CRITICAL", 200),  # fora
    ("WARNING", 40),  # fora da janela de 30d
    ("WARNING", 5),
    ("INFO", 20),  # fora da janela de 14d
    ("INFO", 1),
    ("DEBUG", 8),  # fora da janela de 7d
    ("NOTICE", 30),  # nível fora do vocabulário → fallback de 14d, fora
    ("NOTICE", 3),
]


@pytest.fixture
def conn(tmp_path):
    conexao = libsql.connect(database=str(tmp_path / "logs.db"))
    apply_migrations(conexao)
    conexao.executemany(
        "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                (AGORA - timedelta(days=dias)).isoformat(timespec="milliseconds"),
                "ops-centro",
                None,
                None,
                nivel,
                f"{nivel} de {dias}d",
                None,
            )
            for nivel, dias in AMOSTRA
        ],
    )
    conexao.commit()
    yield conexao
    conexao.close()


def niveis(conn) -> list[tuple[str, str]]:
    linhas = conn.execute("SELECT level, message FROM logs ORDER BY id").fetchall()
    return [tuple(linha) for linha in linhas]


# --- migration -----------------------------------------------------------------
def test_migration_cria_o_indice_de_suporte(tmp_path):
    """Sem (level, timestamp) o DELETE da retenção vira full scan — e row read é
    exatamente a cota do free tier que a retenção deveria proteger."""
    conexao = libsql.connect(database=str(tmp_path / "novo.db"))
    aplicadas = apply_migrations(conexao)
    assert "0002_logs_retention" in aplicadas
    indices = {row[1] for row in conexao.execute("PRAGMA index_list(logs)").fetchall()}
    assert "idx_logs_level_time" in indices


# --- política -------------------------------------------------------------------
def test_janela_por_nivel_e_o_default_documentado():
    policy = RetentionPolicy()
    assert policy.per_level["ERROR"] == 90
    assert policy.per_level["CRITICAL"] == 90
    assert policy.per_level["WARNING"] == 30
    assert policy.per_level["INFO"] == 14
    assert policy.per_level["DEBUG"] == 7
    assert policy.fallback_days == 14


def test_env_sobrescreve_so_o_que_aparece(monkeypatch):
    monkeypatch.setenv("TURSO_LOG_RETENTION_DAYS", "ERROR=120,warn=45")
    policy = RetentionPolicy.from_env()
    assert policy.per_level["ERROR"] == 120
    assert policy.per_level["WARNING"] == 45  # alias WARN normalizado
    assert policy.per_level["INFO"] == 14  # intocado


def test_env_malformado_nao_derruba_o_job(monkeypatch):
    """Job de limpeza que não roda por typo em env var é pior que job com janela padrão."""
    monkeypatch.setenv("TURSO_LOG_RETENTION_DAYS", "ERROR=muitos,INFO=7,lixo")
    policy = RetentionPolicy.from_env()
    assert policy.per_level["ERROR"] == 90
    assert policy.per_level["INFO"] == 7


def test_buckets_cobrem_todos_os_niveis_e_o_resto():
    buckets = RetentionPolicy().buckets(AGORA)
    assert {b.level for b in buckets} == {"CRITICAL", "DEBUG", "ERROR", "INFO", "WARNING",
                                          r.OTHER_LEVELS}
    outros = next(b for b in buckets if b.level == r.OTHER_LEVELS)
    assert "NOT IN" in outros.where


def test_buckets_da_mesma_passada_usam_o_mesmo_now():
    """Cortes calculados com `datetime.now()` por bucket ficariam milissegundos
    diferentes entre si — fronteira de janela tem que ser uma só por execução."""
    buckets = RetentionPolicy(per_level={"ERROR": 30, "INFO": 30}).buckets()
    assert len({b.cutoff for b in buckets if b.days == 30}) == 1


# --- purge -----------------------------------------------------------------------
def test_dry_run_conta_sem_apagar(conn):
    resultado = purge(conn, RetentionPolicy(), dry_run=True, now=AGORA)
    assert resultado.dry_run
    assert resultado.total_deleted == 6
    assert resultado.deleted_by_level == {
        "CRITICAL": 1, "DEBUG": 1, "ERROR": 1, "INFO": 1, "WARNING": 1, r.OTHER_LEVELS: 1,
    }
    assert len(niveis(conn)) == len(AMOSTRA)  # nada saiu
    assert resultado.rows_after == resultado.rows_before


def test_purge_apaga_so_o_que_passou_da_janela_do_nivel(conn):
    resultado = purge(conn, RetentionPolicy(), now=AGORA)
    assert resultado.total_deleted == 6
    assert niveis(conn) == [
        ("ERROR", "ERROR de 10d"),
        ("WARNING", "WARNING de 5d"),
        ("INFO", "INFO de 1d"),
        ("NOTICE", "NOTICE de 3d"),
    ]
    assert resultado.rows_before == 10 and resultado.rows_after == 4


def test_nivel_fora_do_vocabulario_cai_no_fallback(conn):
    """NOTICE de 30d sai (fallback 14d); NOTICE de 3d fica."""
    purge(conn, RetentionPolicy(), now=AGORA)
    restantes = {msg for _, msg in niveis(conn) if msg.startswith("NOTICE")}
    assert restantes == {"NOTICE de 3d"}


def test_lote_pequeno_apaga_tudo_do_mesmo_jeito(conn):
    """O batching existe para não abrir transação gigante; não pode mudar o resultado."""
    resultado = purge(conn, RetentionPolicy(), batch=1, now=AGORA)
    assert resultado.total_deleted == 6
    assert len(niveis(conn)) == 4


def test_janela_customizada_muda_o_que_sai(conn):
    """ERROR de 100d sobrevive com janela de 365d."""
    resultado = purge(conn, RetentionPolicy(per_level={"ERROR": 365}, fallback_days=3650),
                      now=AGORA)
    assert resultado.total_deleted == 0
    assert len(niveis(conn)) == len(AMOSTRA)


def test_purge_e_idempotente(conn):
    primeira = purge(conn, RetentionPolicy(), now=AGORA)
    segunda = purge(conn, RetentionPolicy(), now=AGORA)
    assert primeira.total_deleted == 6
    assert segunda.total_deleted == 0


def test_relatorio_traz_tamanho_e_fracao_do_teto(conn):
    resultado = purge(conn, RetentionPolicy(), now=AGORA, size_limit_bytes=1_000_000)
    assert resultado.db_bytes and resultado.db_bytes > 0
    assert resultado.size_fraction == resultado.db_bytes / 1_000_000
    assert "linha(s) removida(s)" in resultado.summary()


def test_tamanho_indisponivel_vira_none_e_nao_zero():
    """Zero seria uma mentira em que o alerta de teto acreditaria."""

    class SemPragma:
        def execute(self, sql, *args):
            raise RuntimeError("pragma não suportado neste backend")

    assert r.database_bytes(SemPragma()) is None


def test_vacuum_indisponivel_nao_reprova_a_passada(conn):
    """O backend remoto do Turso pode recusar VACUUM — e recusa não é falha do job."""

    class SemVacuum:
        """Proxy da conexão (os atributos do objeto libsql são read-only)."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("VACUUM"):
                raise RuntimeError("VACUUM não permitido no protocolo remoto")
            return self._inner.execute(sql, *args)

        def __getattr__(self, nome):
            return getattr(self._inner, nome)

    resultado = purge(SemVacuum(conn), RetentionPolicy(), vacuum=True, now=AGORA)
    assert resultado.total_deleted == 6  # as linhas já saíram
    assert resultado.vacuumed is False


# --- métricas do próprio job -------------------------------------------------------
def test_sem_endpoint_otlp_a_exportacao_e_noop(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert r.emit_metrics(r.PurgeResult(deleted_by_level={"ERROR": 3})) is False


def test_dry_run_nao_exporta_metricas(monkeypatch):
    """Simulação não pode mexer nas séries que os alertas observam."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://exemplo.invalido/otlp")
    assert r.emit_metrics(r.PurgeResult(deleted_by_level={"ERROR": 3}, dry_run=True)) is False


def test_headers_otlp_sao_url_decodificados():
    headers = r._parse_otlp_headers("Authorization=Basic%20abc123,X-Scope=orgs%2F1")
    assert headers == {"Authorization": "Basic abc123", "X-Scope": "orgs/1"}


# --- CLI ----------------------------------------------------------------------------
def test_cli_dry_run_contra_banco_local(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TURSO_LOG_RETENTION_DAYS", raising=False)
    caminho = str(tmp_path / "cli.db")
    conexao = libsql.connect(database=caminho)
    apply_migrations(conexao)
    conexao.close()

    assert r.main(["--dry-run", "--database", caminho]) == 0
    saida = capsys.readouterr().out
    assert "política:" in saida
    assert "DRY-RUN" in saida


def test_cli_sem_banco_configurado_sai_com_erro(monkeypatch, capsys):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    assert r.main([]) == 2
    assert "TURSO_DATABASE_URL" in capsys.readouterr().out
