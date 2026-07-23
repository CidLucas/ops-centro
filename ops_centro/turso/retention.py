"""Política de retenção e job de limpeza da tabela `logs` (issue #9).

Risco §10 do plano: "custo do Turso crescer com volume de logs". A mitigação é uma
janela de retenção **por nível** — um ERROR de 60 dias atrás ainda ajuda a investigar
reincidência; um INFO de 60 dias atrás só ocupa storage:

    CRITICAL/ERROR  90 dias
    WARNING         30 dias
    INFO            14 dias   (mesma janela do Loki no free tier — RNF03)
    DEBUG            7 dias
    outros níveis   14 dias   (fallback)

Sobrescreva com `TURSO_LOG_RETENTION_DAYS="ERROR=120,WARNING=45,INFO=7"` (ver .env.example);
o que não aparecer na env var mantém o default acima.

O job apaga em lotes (`--batch`, default 5.000) em vez de um `DELETE` único: transação
grande no Turso é o caminho mais curto para timeout, e um lote interrompido só significa
que o próximo dia apaga o resto. `VACUUM` é **opt-in** (`--vacuum`): apagar linha devolve
espaço à free list do SQLite, não ao disco, mas o VACUUM reescreve o banco inteiro — caro
e nem sempre permitido no protocolo remoto do Turso. Rode-o eventualmente, não todo dia.

O próprio job é observado (o observador também é observado): linhas removidas por nível,
duração e tamanho do banco vão por OTLP como as métricas `ops_centro_log_retention_*` e
`ops_centro_logs_*` do catálogo em `ops_centro/metrics.py`. O alerta de teto do free tier
mora em `grafana/alerts/turso-retencao.yaml` e dispara em cima de `ops_centro_logs_db_bytes`.

    uv run python -m ops_centro.turso.retention --dry-run   # só conta o que sairia
    uv run python -m ops_centro.turso.retention             # aplica
    uv run python -m ops_centro.turso.retention --vacuum --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ops_centro import __version__
from ops_centro.conventions import APP_OPS_CENTRO, build_resource_attributes
from ops_centro.metrics import common_labels
from ops_centro.turso.connection import TursoNotConfigured, connect
from ops_centro.turso.log_writer import normalize_level

logger = logging.getLogger(__name__)

# Janela por nível, em dias (ver docstring). CRITICAL acompanha ERROR: os dois são o
# material de investigação de incidente.
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "CRITICAL": 90,
    "ERROR": 90,
    "WARNING": 30,
    "INFO": 14,
    "DEBUG": 7,
}
DEFAULT_FALLBACK_DAYS = 14
DEFAULT_BATCH = 5_000

# Rótulo do bucket que pega níveis fora do vocabulário (label da métrica, por isso
# ASCII e sem espaço).
OTHER_LEVELS = "other"

# Teto de storage do free tier do Turso (~9 GB, docs/free-tier-baseline.md). Só é usado
# para reportar a fração ocupada — o alerta de verdade vive no Grafana.
DEFAULT_SIZE_LIMIT_BYTES = 9 * 1024**3


@dataclass(frozen=True, slots=True)
class Bucket:
    """Uma faixa de exclusão: predicado SQL + parâmetros + rótulo para a métrica."""

    level: str
    days: int
    where: str
    params: tuple[Any, ...]
    cutoff: str


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Janela de retenção por nível, com fallback para níveis não enumerados."""

    per_level: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RETENTION_DAYS))
    fallback_days: int = DEFAULT_FALLBACK_DAYS

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        """Lê `TURSO_LOG_RETENTION_DAYS` ("ERROR=120,INFO=7") sobre os defaults.

        Entrada malformada não derruba o job: o par é ignorado com aviso e o default
        do nível vale — um job de limpeza que não roda por causa de typo em env var é
        pior que um job que roda com a janela padrão.
        """
        policy = dict(DEFAULT_RETENTION_DAYS)
        raw = os.environ.get("TURSO_LOG_RETENTION_DAYS", "").strip()
        for parte in (p for p in raw.split(",") if p.strip()):
            nivel, _, dias = parte.partition("=")
            try:
                policy[normalize_level(nivel)] = int(dias)
            except ValueError:
                logger.warning("trecho inválido em TURSO_LOG_RETENTION_DAYS: %r", parte)
        try:
            fallback = int(os.environ.get("TURSO_LOG_RETENTION_FALLBACK_DAYS", ""))
        except ValueError:
            fallback = DEFAULT_FALLBACK_DAYS
        return cls(per_level=policy, fallback_days=fallback)

    def cutoff(self, days: int, now: datetime | None = None) -> str:
        """Fronteira da janela como string ISO-8601 UTC.

        Comparar timestamp como texto só é correto porque o writer grava sempre no
        mesmo formato (`isoformat(timespec="milliseconds")`, offset `+00:00`): mesmo
        comprimento e mesmo offset ⇒ ordem lexicográfica == ordem cronológica.
        """
        agora = now or datetime.now(timezone.utc)
        return (agora - timedelta(days=days)).isoformat(timespec="milliseconds")

    def buckets(self, now: datetime | None = None) -> list[Bucket]:
        """Faixas de exclusão, uma por nível + a de fallback para o resto.

        `now` é fixado uma vez aqui: com o default (`datetime.now()`) recalculado por
        bucket, dois buckets da mesma passada teriam fronteiras milissegundos diferentes.
        """
        now = now or datetime.now(timezone.utc)
        buckets = []
        for nivel, dias in sorted(self.per_level.items()):
            corte_nivel = self.cutoff(dias, now)
            buckets.append(
                Bucket(
                    level=nivel,
                    days=dias,
                    where="level = ? AND timestamp < ?",
                    params=(nivel, corte_nivel),
                    cutoff=corte_nivel,
                )
            )
        conhecidos = sorted(self.per_level)
        placeholders = ", ".join("?" for _ in conhecidos)
        corte = self.cutoff(self.fallback_days, now)
        buckets.append(
            Bucket(
                level=OTHER_LEVELS,
                days=self.fallback_days,
                where=f"level NOT IN ({placeholders}) AND timestamp < ?",
                params=(*conhecidos, corte),
                cutoff=corte,
            )
        )
        return buckets

    def describe(self) -> str:
        linhas = [f"{nivel}: {dias}d" for nivel, dias in sorted(self.per_level.items())]
        return ", ".join(linhas) + f", demais: {self.fallback_days}d"


@dataclass
class PurgeResult:
    """O que uma passada do job fez — vira métrica OTLP e linha de log de execução."""

    deleted_by_level: dict[str, int] = field(default_factory=dict)
    rows_before: int = 0
    rows_after: int = 0
    db_bytes: int | None = None
    size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES
    duration_seconds: float = 0.0
    vacuumed: bool = False
    dry_run: bool = False
    policy: str = ""

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted_by_level.values())

    @property
    def size_fraction(self) -> float | None:
        """Fração do teto do free tier ocupada (None se o tamanho não foi legível)."""
        if self.db_bytes is None or not self.size_limit_bytes:
            return None
        return self.db_bytes / self.size_limit_bytes

    def summary(self) -> str:
        tamanho = "desconhecido" if self.db_bytes is None else f"{self.db_bytes / 1e6:.1f} MB"
        fracao = "" if self.size_fraction is None else f" ({self.size_fraction:.1%} do teto)"
        modo = "DRY-RUN — nada apagado" if self.dry_run else "aplicado"
        por_nivel = ", ".join(f"{k}={v}" for k, v in sorted(self.deleted_by_level.items()) if v)
        return (
            f"retenção [{modo}] {self.total_deleted} linha(s) removida(s)"
            f"{' (' + por_nivel + ')' if por_nivel else ''} · "
            f"{self.rows_before} → {self.rows_after} linhas · banco {tamanho}{fracao} · "
            f"{self.duration_seconds:.2f}s{' · VACUUM' if self.vacuumed else ''}"
        )


# --- leitura de estado do banco -------------------------------------------------
def table_rows(conn: Any) -> int:
    """Linhas na tabela `logs` (0 se a tabela ainda não existe)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
    except Exception as exc:  # noqa: BLE001 — driver pode levantar qualquer coisa
        logger.warning("não foi possível contar as linhas de `logs`: %s", exc)
        return 0


def database_bytes(conn: Any) -> int | None:
    """Tamanho do banco em bytes, ou None quando o backend não expõe os pragmas.

    `page_count * page_size` é a medida barata (não varre linha nenhuma). O protocolo
    remoto do Turso pode recusar pragmas — daí o None, que o chamador reporta como
    "desconhecido" em vez de tratar como zero.
    """
    try:
        row = conn.execute(
            "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.info("tamanho do banco indisponível neste backend: %s", exc)
        return None


def _delete_bucket(conn: Any, bucket: Bucket, batch: int) -> int:
    """Apaga um bucket em lotes; devolve quantas linhas saíram.

    O `DELETE ... WHERE id IN (SELECT id ... LIMIT ?)` é o que mantém a transação
    pequena. `cursor.rowcount` do libsql local é confiável; num backend que devolva -1
    o loop para no primeiro lote e a passada seguinte continua de onde parou — mais
    lento, nunca incorreto.
    """
    removidas = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE {bucket.where} "
            f"ORDER BY id LIMIT ?)",
            (*bucket.params, batch),
        )
        conn.commit()
        afetadas = getattr(cursor, "rowcount", -1)
        if afetadas is None or afetadas < 0:
            logger.debug("driver sem rowcount confiável — encerrando o bucket %s", bucket.level)
            break
        removidas += afetadas
        if afetadas < batch:
            break
    return removidas


def purge(
    conn: Any,
    policy: RetentionPolicy | None = None,
    *,
    batch: int = DEFAULT_BATCH,
    dry_run: bool = False,
    vacuum: bool = False,
    now: datetime | None = None,
    size_limit_bytes: int | None = None,
) -> PurgeResult:
    """Aplica a política de retenção na conexão dada.

    `dry_run` conta o que sairia sem apagar — é o modo de conferir uma mudança de
    janela antes de ela virar exclusão irreversível.
    """
    policy = policy or RetentionPolicy()
    inicio = time.perf_counter()
    resultado = PurgeResult(
        rows_before=table_rows(conn),
        dry_run=dry_run,
        policy=policy.describe(),
        size_limit_bytes=size_limit_bytes or _size_limit_from_env(),
    )

    for bucket in policy.buckets(now):
        if dry_run:
            linha = conn.execute(
                f"SELECT COUNT(*) FROM logs WHERE {bucket.where}", bucket.params
            ).fetchone()
            resultado.deleted_by_level[bucket.level] = int(linha[0]) if linha else 0
        else:
            resultado.deleted_by_level[bucket.level] = _delete_bucket(conn, bucket, batch)

    if vacuum and not dry_run and resultado.total_deleted:
        try:
            conn.execute("VACUUM")
            conn.commit()
            resultado.vacuumed = True
        except Exception as exc:  # noqa: BLE001
            # Backend remoto pode recusar VACUUM. Não é falha do job: as linhas já saíram.
            logger.warning("VACUUM indisponível neste backend (linhas já removidas): %s", exc)

    resultado.rows_after = resultado.rows_before if dry_run else table_rows(conn)
    resultado.db_bytes = database_bytes(conn)
    resultado.duration_seconds = time.perf_counter() - inicio
    return resultado


def _size_limit_from_env() -> int:
    try:
        return int(os.environ.get("TURSO_DB_SIZE_LIMIT_BYTES", "")) or DEFAULT_SIZE_LIMIT_BYTES
    except ValueError:
        return DEFAULT_SIZE_LIMIT_BYTES


# --- emissão das métricas do próprio job (OTLP) ---------------------------------
def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """`Authorization=Basic%20xxx,foo=bar` → dict, com os valores URL-decodificados.

    Mesmo formato que a `blu_observability_bootstrap` consome; replicado aqui (em 6
    linhas) porque o job é um one-shot de CLI e não deve arrastar FastAPI só para
    exportar quatro números.
    """
    from urllib.parse import unquote

    headers = {}
    for parte in (p for p in raw.split(",") if "=" in p):
        chave, _, valor = parte.partition("=")
        headers[chave.strip()] = unquote(valor.strip())
    return headers


def emit_metrics(result: PurgeResult, *, timeout_millis: int = 10_000) -> bool:
    """Publica as métricas do job via OTLP. Retorna False (sem levantar) quando não há
    endpoint configurado ou o SDK não está instalado — o job de limpeza nunca falha por
    causa da telemetria dele.

    Um `MeterProvider` próprio, com flush explícito no fim: processo one-shot não vive o
    suficiente para o exportador periódico acordar sozinho.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint or result.dry_run:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.info("SDK de métricas do OTel ausente — job sem telemetria própria")
        return False

    atributos = build_resource_attributes(APP_OPS_CENTRO, version=__version__)
    resource = Resource.create({"service.name": APP_OPS_CENTRO, **atributos})
    exporter = OTLPMetricExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/metrics",
        headers=_parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")),
    )
    provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)],
    )
    # RF02 vai como atributo do **ponto**, não só do Resource: a ingestão OTLP do Grafana
    # Cloud não promove resource attribute a label de métrica (ver common_labels()).
    comuns = common_labels(APP_OPS_CENTRO)
    try:
        meter = provider.get_meter("ops_centro.turso.retention")
        removidas = meter.create_counter(
            "ops_centro_log_retention_deleted_total",
            description="Linhas removidas pelo job de retenção, por nível",
        )
        # Inclusive os zeros: counter que nunca incrementou não cria série, e aí o painel
        # mostra "No data" onde o certo é 0 — indistinguível de "o job não rodou", que é
        # justamente o que o alerta `ops-centro-retencao-parada` precisa diferenciar.
        for nivel, quantidade in result.deleted_by_level.items():
            removidas.add(quantidade, {**comuns, "level": nivel})
        meter.create_histogram(
            "ops_centro_log_retention_duration_seconds",
            unit="s",
            description="Duração de uma execução do job de retenção",
        ).record(result.duration_seconds, comuns)
        meter.create_gauge(
            "ops_centro_logs_rows", description="Linhas na tabela `logs` após a retenção"
        ).set(result.rows_after, comuns)
        if result.db_bytes is not None:
            meter.create_gauge(
                "ops_centro_logs_db_bytes",
                unit="By",
                description="Tamanho do banco de logs no Turso",
            ).set(result.db_bytes, comuns)
        return bool(provider.force_flush(timeout_millis=timeout_millis))
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha ao exportar as métricas do job de retenção: %s", exc)
        return False
    finally:
        try:
            provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro no shutdown do MeterProvider: %s", exc)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Retenção/limpeza dos logs no Turso (issue #9)")
    parser.add_argument("--dry-run", action="store_true", help="só conta o que seria apagado")
    parser.add_argument("--vacuum", action="store_true", help="roda VACUUM ao final (caro)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="linhas por lote")
    parser.add_argument("--database", help="URL/caminho alternativo (default: TURSO_DATABASE_URL)")
    parser.add_argument("--json", action="store_true", help="saída JSON (para o log da run)")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.database) if args.database else connect()
    except TursoNotConfigured as exc:
        print(f"erro: {exc}")
        return 2

    try:
        resultado = purge(
            conn,
            RetentionPolicy.from_env(),
            batch=max(1, args.batch),
            dry_run=args.dry_run,
            vacuum=args.vacuum,
        )
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("erro ao fechar a conexão: %s", exc)

    exportou = emit_metrics(resultado)

    if args.json:
        print(json.dumps({**asdict(resultado), "metrics_exported": exportou}, ensure_ascii=False,
                         indent=2))
    else:
        print(f"política: {resultado.policy}")
        print(resultado.summary())
        if not exportou and not args.dry_run:
            print("(métricas não exportadas — OTEL_EXPORTER_OTLP_ENDPOINT ausente)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
