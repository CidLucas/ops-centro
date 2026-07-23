"""Catálogo das métricas prioritárias da §7 do plano (issue #11).

Fecha o vão entre "o trace existe" e "a métrica agregada é consultável barato": no free
tier, varrer traces no Tempo para responder "qual a taxa de erro da tool X?" custa muito
mais que ler uma série no Mimir. Cada item da §7 vira aqui um instrumento nomeado, com
tipo, unidade e **labels fechadas** — é o mesmo papel que `conventions.py` cumpre para
spans e atributos.

A instrumentação em si vive nos repos dos apps (cross-repo, como diz a issue). O que este
módulo garante é que os três lados falem do mesmo nome:

- **apps** — replicam os nomes daqui ao criar counter/histogram (mesma regra de réplica
  do [schema.md §4](../docs/schema.md));
- **dashboards** — `ops_centro.grafana.dashboards` monta os painéis a partir do catálogo,
  então painel que referencia métrica fora dele quebra no teste;
- **validação** — `python -m ops_centro.metrics --check` consulta o Prometheus/Mimir e diz,
  métrica por métrica, se já está chegando com os labels comuns (critério de aceite do #11).

Cardinalidade é regra, não recomendação: `validate_catalog()` rejeita label fora de
`conventions.ALLOWED_METRIC_LABELS` e `tenant_id` fora das métricas de volume — o teste
`tests/test_metrics.py` roda essa validação sobre o catálogo inteiro.

    uv run python -m ops_centro.metrics            # lista o catálogo
    uv run python -m ops_centro.metrics --check    # confere a emissão no Grafana Cloud
    uv run python -m ops_centro.metrics --check --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Iterable

from ops_centro.conventions import (
    ALLOWED_METRIC_LABELS,
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    APP_OPS_CENTRO,
    ATTR_APP_NAME,
    ATTR_ENVIRONMENT,
    ATTR_TENANT_ID,
    ENV_DEV,
    METRIC_PREFIXES,
)

# --- itens da §7 do plano ------------------------------------------------------
# O texto é o do plano; o catálogo referencia estas constantes para que a cobertura
# ("todo item da §7 tem pelo menos uma métrica") seja verificável por teste.
ITEM_ERROR_RATE = "taxa de erro por agente / por tool"
ITEM_LATENCY = "latência p50/p95/p99 de MCP tools e chamadas LLM"
ITEM_INGESTION_FAILURE = "taxa de falha na ingestão de arquivos"
ITEM_MEMORY_QUERY_LATENCY = "latência de queries MCP do serviço de memória"
ITEM_TENANT_VOLUME = "volume por tenant (execuções, arquivos processados)"

PLANO_ITEMS = (
    ITEM_ERROR_RATE,
    ITEM_LATENCY,
    ITEM_INGESTION_FAILURE,
    ITEM_MEMORY_QUERY_LATENCY,
    ITEM_TENANT_VOLUME,
)

# Métricas de operação do próprio pipeline (o observador também é observado). Não são
# da §7 — ficam no catálogo porque os dashboards e o alerta de teto dependem delas.
ITEM_SELF = "operação do próprio ops-centro"

COUNTER = "counter"
HISTOGRAM = "histogram"
GAUGE = "gauge"

# Estado de adoção. `emitting` = o app já publica a série hoje; `pending` = depende da
# instrumentação nos repos dos apps (#5/#7) — o checker distingue os dois para não
# transformar trabalho pendente conhecido em falha vermelha sem informação.
EMITTING = "emitindo"
PENDING = "pendente"


@dataclass(frozen=True, slots=True)
class Metric:
    """Um instrumento do catálogo.

    `labels` são as labels **próprias** do instrumento: `app_name`/`environment` vêm do
    Resource em todo sinal (RF02) e por isso não se repetem aqui.
    """

    name: str
    app: str
    kind: str
    unit: str
    labels: tuple[str, ...]
    item: str
    description: str
    status: str = PENDING

    @property
    def is_priority(self) -> bool:
        """True para métricas que atendem um item da §7 (exclui as de operação)."""
        return self.item in PLANO_ITEMS

    def selector(self, extra: str = "") -> str:
        """Seletor PromQL da série, opcionalmente com filtros extras."""
        return f"{self.name}{{{extra}}}" if extra else self.name

    def sample_series(self) -> str:
        """Nome da série que existe de fato no Prometheus (histograma vira `_bucket`)."""
        return f"{self.name}_bucket" if self.kind == HISTOGRAM else self.name


# --- catálogo ------------------------------------------------------------------
# Ordem: por app, e dentro do app do sinal mais agregado para o mais específico.
CATALOG: tuple[Metric, ...] = (
    # ---------------------------------------------------------- agents-platform --
    Metric(
        name="agents_platform_agent_executions_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("agent", "status"),
        item=ITEM_ERROR_RATE,
        description="Execuções de agente concluídas, por agente e desfecho (ok|error).",
    ),
    Metric(
        name="agents_platform_agent_execution_duration_seconds",
        app=APP_AGENTS_PLATFORM,
        kind=HISTOGRAM,
        unit="s",
        labels=("agent",),
        item=ITEM_LATENCY,
        description="Duração ponta a ponta do span agent_execution.",
    ),
    Metric(
        name="agents_platform_tool_calls_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("tool", "status"),
        item=ITEM_ERROR_RATE,
        description="Chamadas de tool MCP, por tool e desfecho — base da taxa de erro por tool.",
    ),
    Metric(
        name="agents_platform_tool_call_duration_seconds",
        app=APP_AGENTS_PLATFORM,
        kind=HISTOGRAM,
        unit="s",
        labels=("tool",),
        item=ITEM_LATENCY,
        description="Latência de chamadas de tool MCP (p50/p95/p99 por tool).",
    ),
    Metric(
        name="agents_platform_llm_calls_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("model", "status"),
        item=ITEM_ERROR_RATE,
        description="Chamadas ao provedor LLM, por modelo e desfecho.",
    ),
    Metric(
        name="agents_platform_llm_call_duration_seconds",
        app=APP_AGENTS_PLATFORM,
        kind=HISTOGRAM,
        unit="s",
        labels=("model",),
        item=ITEM_LATENCY,
        description="Latência de chamadas LLM (p50/p95/p99 por modelo).",
    ),
    # Tokens de entrada e saída em counters separados de propósito: uma label `kind`
    # para distinguir os dois dobraria a série sem responder nenhuma pergunta que os
    # dois nomes já não respondam — e o vocabulário de labels fica menor.
    Metric(
        name="agents_platform_llm_tokens_input_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("model",),
        item=ITEM_LATENCY,
        description="Tokens de entrada consumidos, por modelo (drill-down de custo).",
    ),
    Metric(
        name="agents_platform_llm_tokens_output_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("model",),
        item=ITEM_LATENCY,
        description="Tokens de saída gerados, por modelo.",
    ),
    Metric(
        name="agents_platform_llm_cost_usd_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=("model",),
        item=ITEM_LATENCY,
        description="Custo acumulado em USD, por modelo (só quando calculável).",
    ),
    # Volume por tenant fica em contador próprio, e não como label extra em
    # `agent_executions_total`: multiplicar tenant × agent × status é justamente o
    # jeito de estourar as 10k séries ativas do free tier.
    Metric(
        name="agents_platform_tenant_executions_total",
        app=APP_AGENTS_PLATFORM,
        kind=COUNTER,
        unit="1",
        labels=(ATTR_TENANT_ID, "status"),
        item=ITEM_TENANT_VOLUME,
        description="Execuções de agente por tenant — volume de uso (§7).",
    ),
    # ---------------------------------------------------------- file-memory-mcp --
    Metric(
        name="context_mcp_tool_calls_total",
        app=APP_FILE_MEMORY,
        kind=COUNTER,
        unit="1",
        labels=("tool", "status"),
        item=ITEM_ERROR_RATE,
        description="Chamadas de tool MCP do serviço de memória, por tool e desfecho.",
        status=EMITTING,
    ),
    Metric(
        name="context_mcp_tool_call_duration_seconds",
        app=APP_FILE_MEMORY,
        kind=HISTOGRAM,
        unit="s",
        labels=("tool",),
        item=ITEM_LATENCY,
        description="Latência de tool MCP do serviço de memória.",
        status=EMITTING,
    ),
    Metric(
        name="context_mcp_ingestion_stage_total",
        app=APP_FILE_MEMORY,
        kind=COUNTER,
        unit="1",
        labels=("stage", "status"),
        item=ITEM_INGESTION_FAILURE,
        description="Etapas do pipeline de ingestão, por etapa e desfecho — funil e taxa de falha.",
        status=EMITTING,
    ),
    Metric(
        name="context_mcp_ingestion_stage_duration_seconds",
        app=APP_FILE_MEMORY,
        kind=HISTOGRAM,
        unit="s",
        labels=("stage",),
        item=ITEM_INGESTION_FAILURE,
        description="Latência por etapa da ingestão.",
        status=EMITTING,
    ),
    Metric(
        name="context_mcp_memory_queries_total",
        app=APP_FILE_MEMORY,
        kind=COUNTER,
        unit="1",
        labels=("query_type", "status"),
        item=ITEM_MEMORY_QUERY_LATENCY,
        description="Queries MCP de memória, por tipo e desfecho.",
    ),
    Metric(
        name="context_mcp_memory_query_duration_seconds",
        app=APP_FILE_MEMORY,
        kind=HISTOGRAM,
        unit="s",
        labels=("query_type",),
        item=ITEM_MEMORY_QUERY_LATENCY,
        description="Latência de queries MCP de memória (p50/p95/p99 por tipo).",
    ),
    Metric(
        name="context_mcp_tenant_files_total",
        app=APP_FILE_MEMORY,
        kind=COUNTER,
        unit="1",
        labels=(ATTR_TENANT_ID, "status"),
        item=ITEM_TENANT_VOLUME,
        description="Arquivos processados por tenant — volume de uso (§7).",
    ),
    # --------------------------------------------------------------- ops-centro --
    Metric(
        name="ops_centro_alerts_received_total",
        app=APP_OPS_CENTRO,
        kind=COUNTER,
        unit="1",
        labels=("status",),
        item=ITEM_SELF,
        description="Alertas recebidos do Grafana pelo receiver (RF06). `status` segue o "
        "vocabulário ok|error do schema: firing → error, resolved → ok.",
        status=EMITTING,
    ),
    Metric(
        name="ops_centro_log_retention_deleted_total",
        app=APP_OPS_CENTRO,
        kind=COUNTER,
        unit="1",
        labels=("level",),
        item=ITEM_SELF,
        description="Linhas removidas pelo job de retenção do Turso, por nível (issue #9).",
        status=EMITTING,
    ),
    Metric(
        name="ops_centro_log_retention_duration_seconds",
        app=APP_OPS_CENTRO,
        kind=HISTOGRAM,
        unit="s",
        labels=(),
        item=ITEM_SELF,
        description="Duração de uma execução do job de retenção.",
        status=EMITTING,
    ),
    Metric(
        name="ops_centro_logs_db_bytes",
        app=APP_OPS_CENTRO,
        kind=GAUGE,
        unit="By",
        labels=(),
        item=ITEM_SELF,
        description="Tamanho do banco de logs no Turso — base do alerta de teto do free tier.",
        status=EMITTING,
    ),
    Metric(
        name="ops_centro_logs_rows",
        app=APP_OPS_CENTRO,
        kind=GAUGE,
        unit="1",
        labels=(),
        item=ITEM_SELF,
        description="Linhas na tabela `logs` após a passada de retenção.",
        status=EMITTING,
    ),
)

BY_NAME = {metric.name: metric for metric in CATALOG}


def common_labels(app_name: str, environment: str | None = None) -> dict[str, str]:
    """Labels do RF02 que **todo ponto de métrica** precisa carregar.

    Descoberta ao rodar o gate contra o stack real (2026-07-23): a ingestão OTLP do
    Grafana Cloud **não** promove resource attributes a label de métrica. Uma série
    emitida com `app_name`/`environment` só no Resource chega ao Mimir como

        ops_centro_logs_rows{job="ops-centro", service_name="ops-centro"}

    — sem `target_info` para fazer join, e portanto invisível para qualquer query
    cruzada por app/ambiente (RNF05). Em traces e logs o Resource sobrevive como
    structured metadata; em métricas, não.

    A saída é emitir os dois como **atributos do ponto**, que viram label sempre. Custo:
    duas labels enumeráveis por série — as mesmas que `ALLOWED_METRIC_LABELS` já permite.
    `version` fica de fora de propósito: mudaria a cada deploy e criaria série nova.

    Uso nos apps: `counter.add(1, {**common_labels(APP, ), "tool": nome})`.
    """
    env = environment or os.environ.get("ENVIRONMENT", ENV_DEV)
    return {ATTR_APP_NAME: app_name, ATTR_ENVIRONMENT: env}


def by_app(app: str) -> list[Metric]:
    """Métricas do catálogo emitidas por um app."""
    return [metric for metric in CATALOG if metric.app == app]


def by_item(item: str) -> list[Metric]:
    """Métricas que atendem um item da §7."""
    return [metric for metric in CATALOG if metric.item == item]


def priority_metrics() -> list[Metric]:
    """Só as métricas da §7 (exclui as de operação do próprio ops-centro)."""
    return [metric for metric in CATALOG if metric.is_priority]


# --- invariantes do catálogo ---------------------------------------------------
def validate_catalog(catalog: Iterable[Metric] = CATALOG) -> list[str]:
    """Confere as regras do [schema.md §3] sobre o catálogo. Devolve a lista de
    violações (vazia = catálogo íntegro) em vez de levantar: o chamador (teste ou CLI)
    decide o que fazer, e o relatório mostra **todas** as violações de uma vez.
    """
    problemas: list[str] = []
    vistos: set[str] = set()

    for metric in catalog:
        prefix = METRIC_PREFIXES.get(metric.app)
        if prefix is None:
            problemas.append(f"{metric.name}: app desconhecido {metric.app!r}")
        elif not metric.name.startswith(f"{prefix}_"):
            problemas.append(f"{metric.name}: prefixo esperado {prefix}_ (app {metric.app})")

        if metric.name in vistos:
            problemas.append(f"{metric.name}: nome duplicado no catálogo")
        vistos.add(metric.name)

        if metric.kind == COUNTER and not metric.name.endswith("_total"):
            problemas.append(f"{metric.name}: counter deve terminar em _total")
        if metric.kind == HISTOGRAM and not metric.name.endswith(("_duration_seconds", "_bytes")):
            problemas.append(
                f"{metric.name}: histograma deve terminar em _duration_seconds ou _bytes"
            )
        if metric.kind == GAUGE and metric.name.endswith("_total"):
            problemas.append(f"{metric.name}: _total é sufixo de counter, não de gauge")
        if metric.name.endswith("_duration_seconds") and metric.unit != "s":
            problemas.append(f"{metric.name}: latência deve ter unit 's' (nada de _ms)")
        if metric.name.endswith("_ms") or "_millis" in metric.name:
            problemas.append(f"{metric.name}: unidade de tempo é sempre segundos")

        fora = [label for label in metric.labels if label not in ALLOWED_METRIC_LABELS]
        if fora:
            problemas.append(
                f"{metric.name}: label(s) fora do schema: {', '.join(fora)} "
                f"(permitidas: {', '.join(sorted(ALLOWED_METRIC_LABELS))})"
            )
        if ATTR_TENANT_ID in metric.labels and metric.item != ITEM_TENANT_VOLUME:
            problemas.append(
                f"{metric.name}: tenant_id só é permitido em métrica de volume/uso "
                f"(§7) — esta é de '{metric.item}'"
            )

    faltando = [item for item in PLANO_ITEMS if not any(m.item == item for m in catalog)]
    problemas += [f"item da §7 sem métrica no catálogo: {item}" for item in faltando]
    return problemas


# --- verificação contra o Grafana Cloud ----------------------------------------
def check_catalog(cloud, apps: list[str] | None = None, window: str = "1h") -> list:
    """Confere, métrica a métrica, se a série existe no Prometheus/Mimir com os labels
    comuns do RF02 (critério de aceite do #11).

    Reaproveita a maquinaria de `ops_centro.validation` (endpoints, tratamento de erro,
    formato do relatório) — o gate do #11 é o mesmo tipo de checagem auditável do #6:
    imprime a query que rodou, para colar no Explore.

    Métrica ainda `pendente` de instrumentação vira SKIP com o motivo, não FALHA: o que
    o gate mede é deriva entre catálogo e realidade, não backlog conhecido.
    """
    from ops_centro.validation import CheckResult, _request, _skip

    apps = apps or sorted({metric.app for metric in CATALOG})
    resultados: list[CheckResult] = []

    for metric in (m for m in CATALOG if m.app in apps):
        # `count by (labels)` responde as duas perguntas de uma vez: a série existe e
        # quais labels ela carrega. Um `count(...)` puro esconderia label faltando.
        agrupamento = ", ".join((ATTR_APP_NAME, ATTR_ENVIRONMENT, *metric.labels))
        query = f"count by ({agrupamento}) ({metric.sample_series()})"

        if metric.status == PENDING:
            resultados.append(
                _skip(
                    "métricas",
                    metric.name,
                    query,
                    f"instrumentação pendente no repo de {metric.app} — nada a cobrar ainda",
                )
            )
            continue

        def _evaluate(body: dict, _metric: Metric = metric) -> tuple[bool, str]:
            series = [item.get("metric", {}) for item in body.get("data", {}).get("result", [])]
            if not series:
                return False, "nenhuma série — o app não está emitindo esta métrica"
            esperadas = (ATTR_APP_NAME, ATTR_ENVIRONMENT, *_metric.labels)
            ausentes = [
                label for label in esperadas if not all(serie.get(label) for serie in series)
            ]
            if ausentes:
                return False, f"{len(series)} série(s), mas sem label(s): {', '.join(ausentes)}"
            extras = {
                label
                for serie in series
                for label in serie
                if label not in ALLOWED_METRIC_LABELS and not label.startswith("__")
            }
            # Label fora do schema é o sintoma clássico de explosão de cardinalidade —
            # avisa sem reprovar, porque o Grafana Cloud injeta labels próprias.
            sufixo = f" · labels fora do schema: {', '.join(sorted(extras))}" if extras else ""
            return True, f"{len(series)} série(s) com todos os labels{sufixo}"

        resultados.append(
            _request(
                cloud.prom,
                "métricas",
                metric.name,
                "/api/v1/query",
                {"query": query},
                _evaluate,
                query,
            )
        )
    return resultados


def _print_catalog() -> None:
    largura = max(len(metric.name) for metric in CATALOG)
    for item in (*PLANO_ITEMS, ITEM_SELF):
        print(f"\n§7 — {item}" if item != ITEM_SELF else f"\n{ITEM_SELF}")
        for metric in by_item(item):
            labels = ", ".join(metric.labels) or "—"
            print(
                f"  {metric.name:<{largura}}  {metric.kind:<9} [{metric.unit:>2}] "
                f"{metric.status:<8} labels: {labels}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catálogo de métricas da §7 (issue #11)")
    parser.add_argument("--check", action="store_true", help="consulta o Prometheus do stack")
    parser.add_argument("--app", action="append", help="restringe a um app (pode repetir)")
    parser.add_argument("--json", action="store_true", help="saída JSON")
    args = parser.parse_args(argv)

    problemas = validate_catalog()
    if problemas:
        for problema in problemas:
            print(f"[CATÁLOGO INVÁLIDO] {problema}")
        return 2

    if not args.check:
        if args.json:
            print(json.dumps([asdict(m) for m in CATALOG], ensure_ascii=False, indent=2))
        else:
            _print_catalog()
        return 0

    from ops_centro.validation import GrafanaCloud, Summary

    summary = Summary.of(check_catalog(GrafanaCloud.from_env(), args.app))
    if args.json:
        print(
            json.dumps(
                {
                    "passed": summary.passed,
                    "failed": summary.failed,
                    "skipped": summary.skipped,
                    "results": [asdict(r) for r in summary.results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for resultado in summary.results:
            print(resultado.line())
        print(f"\n{summary.passed} ok · {summary.failed} falha(s) · {summary.skipped} pulada(s)")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
