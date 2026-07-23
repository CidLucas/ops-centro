"""Dashboards iniciais as-code (issue #10).

Quatro dashboards, todos com variável `environment` e datasource parametrizado:

| uid                          | Arquivo              | Responde                                    |
| ---------------------------- | -------------------- | ------------------------------------------- |
| `ops-centro-visao-geral`     | `visao-geral.json`   | saúde dos dois apps lado a lado              |
| `ops-centro-por-tenant`      | `por-tenant.json`    | volume e erro por `tenant_id` (§7, RNF05)    |
| `ops-centro-agents-platform` | `agents-platform.json` | drill-down de agent_execution / mcp_tool_call |
| `ops-centro-file-memory`     | `file-memory.json`   | funil de ingestão + latência de query MCP     |

**Os JSONs são gerados, não editados à mão.** Cada painel é montado a partir do catálogo
de métricas da §7 (`ops_centro.metrics`), o que dá duas garantias que um export da UI não
dá: nenhum painel referencia métrica que não existe no schema, e nenhuma query usa label
de alta cardinalidade. `--check` reprova se o JSON commitado divergiu do gerador — é o
que impede "editei na UI e exportei por cima".

    uv run python -m ops_centro.grafana.dashboards --write   # (re)gera grafana/dashboards/
    uv run python -m ops_centro.grafana.dashboards --check   # gate de deriva (roda no CI)
    uv run python -m ops_centro.grafana.dashboards --apply   # publica no Grafana Cloud

Publicar exige um token com `dashboards:write` (`GRAFANA_API_TOKEN`) — o
`GRAFANA_READ_TOKEN` da validação da Fase 1 não serve. A aplicação é idempotente: uid
fixo por dashboard + `overwrite`, então rodar duas vezes não cria cópia.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import httpx

from ops_centro.metrics import BY_NAME

DASHBOARDS_DIR = Path(__file__).resolve().parents[2] / "grafana" / "dashboards"

FOLDER_UID = "ops-centro"
FOLDER_TITLE = "Ops Centro"

# Datasource vem de variável: o mesmo JSON serve para o stack de dev e para outro stack
# amanhã, sem editar uid de datasource em 30 painéis.
DS = {"type": "prometheus", "uid": "${DS_PROM}"}

TAGS = ["ops-centro", "as-code"]

# Filtro comum a todas as queries. `environment` é multi + All (allValue `.*`), então
# `=~` é o operador correto; `app_name` já separa os apps pelo nome da métrica.
ENV = 'environment=~"$environment"'
TENANT = 'tenant_id=~"$tenant_id"'

SEC = "s"
PERCENT = "percent"
OPS = "ops"
SHORT = "short"
BYTES = "bytes"
CURRENCY = "currencyUSD"

# Limiares de cor reaproveitados nos painéis de taxa de erro (%).
ERROR_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "green", "value": None},
        {"color": "yellow", "value": 1},
        {"color": "red", "value": 5},
    ],
}


# --- PromQL --------------------------------------------------------------------
def _filters(*extra: str) -> str:
    return ", ".join([ENV, *(f for f in extra if f)])


def rate_by(metric: str, by: Iterable[str] = (), *extra: str) -> str:
    """Taxa por segundo de um counter, agrupada por labels do schema."""
    agrupamento = f" by ({', '.join(by)}) " if by else ""
    return f"sum{agrupamento}(rate({metric}{{{_filters(*extra)}}}[$__rate_interval]))"


def increase_by(metric: str, by: Iterable[str] = (), window: str = "$__range", *extra: str) -> str:
    """Total acumulado na janela do dashboard (para tabelas e stats)."""
    agrupamento = f" by ({', '.join(by)}) " if by else ""
    return f"sum{agrupamento}(increase({metric}{{{_filters(*extra)}}}[{window}]))"


def error_rate(metric: str, by: Iterable[str] = (), *extra: str) -> str:
    """Percentual de erro de um counter que carrega `status` (ok|error).

    `clamp_min` no denominador evita a divisão por zero virar `NaN` no gráfico quando
    não houve tráfego na janela — sem isso o painel some em vez de mostrar 0%.
    """
    erro = rate_by(metric, by, 'status="error"', *extra)
    total = rate_by(metric, by, *extra)
    return f"100 * {erro} / clamp_min({total}, 1e-9)"


def quantile(metric: str, q: float, by: Iterable[str] = (), *extra: str) -> str:
    """Quantil de um histograma. `le` sempre entra no agrupamento — sem ele o
    `histogram_quantile` recebe um vetor sem os buckets e devolve NaN."""
    agrupamento = ", ".join(("le", *by))
    return (
        f"histogram_quantile({q:.2f}, sum by ({agrupamento}) "
        f"(rate({metric}_bucket{{{_filters(*extra)}}}[$__rate_interval])))"
    )


# --- construção de painéis ------------------------------------------------------
def target(expr: str, legend: str = "", ref: str = "A") -> dict[str, Any]:
    alvo: dict[str, Any] = {"datasource": DS, "expr": expr, "refId": ref}
    if legend:
        alvo["legendFormat"] = legend
    return alvo


def _field_config(unit: str, thresholds: dict | None = None, decimals: int | None = None) -> dict:
    defaults: dict[str, Any] = {"unit": unit, "min": 0, "custom": {}}
    if thresholds:
        defaults["thresholds"] = thresholds
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"defaults": defaults, "overrides": []}


class Layout:
    """Acumula painéis numa grade de 24 colunas, calculando `gridPos` e `id`.

    Grafana exige posição e id explícitos; calcular à mão em 30 painéis é onde um
    dashboard as-code costuma virar sopa de números mágicos.
    """

    def __init__(self) -> None:
        self.panels: list[dict[str, Any]] = []
        self._x = 0
        self._y = 0
        self._linha_h = 0
        self._id = 0

    def _proximo_id(self) -> int:
        self._id += 1
        return self._id

    def row(self, title: str) -> "Layout":
        if self._x or self._linha_h:
            self._y += self._linha_h
            self._x = 0
            self._linha_h = 0
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "id": self._proximo_id(),
                "collapsed": False,
                "panels": [],
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y},
            }
        )
        self._y += 1
        return self

    def add(self, panel: dict[str, Any], *, w: int = 12, h: int = 8) -> "Layout":
        if self._x + w > 24:
            self._y += self._linha_h
            self._x = 0
            self._linha_h = 0
        panel = {**panel, "id": self._proximo_id(), "gridPos": {"h": h, "w": w, "x": self._x, "y": self._y}}
        self.panels.append(panel)
        self._x += w
        self._linha_h = max(self._linha_h, h)
        return self

    def timeseries(
        self,
        title: str,
        targets: list[dict],
        *,
        unit: str = SHORT,
        description: str = "",
        thresholds: dict | None = None,
        w: int = 12,
        h: int = 8,
    ) -> "Layout":
        return self.add(
            {
                "type": "timeseries",
                "title": title,
                "description": description,
                "datasource": DS,
                "targets": targets,
                "fieldConfig": _field_config(unit, thresholds),
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
            },
            w=w,
            h=h,
        )

    def stat(
        self,
        title: str,
        targets: list[dict],
        *,
        unit: str = SHORT,
        description: str = "",
        decimals: int | None = None,
        w: int = 6,
        h: int = 4,
    ) -> "Layout":
        return self.add(
            {
                "type": "stat",
                "title": title,
                "description": description,
                "datasource": DS,
                "targets": targets,
                "fieldConfig": _field_config(unit, decimals=decimals),
                "options": {
                    "colorMode": "value",
                    "graphMode": "area",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                },
            },
            w=w,
            h=h,
        )

    def barchart(
        self, title: str, targets: list[dict], *, unit: str = SHORT, description: str = "",
        w: int = 12, h: int = 8,
    ) -> "Layout":
        return self.add(
            {
                "type": "barchart",
                "title": title,
                "description": description,
                "datasource": DS,
                "targets": [{**t, "instant": True, "format": "table"} for t in targets],
                "fieldConfig": _field_config(unit),
                "options": {"orientation": "horizontal", "xTickLabelRotation": 0},
            },
            w=w,
            h=h,
        )

    def table(
        self, title: str, targets: list[dict], *, unit: str = SHORT, description: str = "",
        w: int = 12, h: int = 8,
    ) -> "Layout":
        return self.add(
            {
                "type": "table",
                "title": title,
                "description": description,
                "datasource": DS,
                "targets": [{**t, "instant": True, "format": "table"} for t in targets],
                "fieldConfig": _field_config(unit),
                "options": {"showHeader": True},
                "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
            },
            w=w,
            h=h,
        )


# --- variáveis ------------------------------------------------------------------
def var_datasource() -> dict[str, Any]:
    return {
        "type": "datasource",
        "name": "DS_PROM",
        "label": "Datasource",
        "query": "prometheus",
        "refresh": 1,
        "hide": 0,
        "regex": "",
        "current": {},
    }


def var_query(
    name: str, label: str, query: str, *, multi: bool = True, all_value: str = ".*"
) -> dict[str, Any]:
    return {
        "type": "query",
        "name": name,
        "label": label,
        "datasource": DS,
        "definition": query,
        "query": query,
        "refresh": 2,  # revalida ao mudar o intervalo de tempo
        "includeAll": True,
        "allValue": all_value,
        "multi": multi,
        "sort": 1,
        "hide": 0,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
    }


def dashboard(
    uid: str, title: str, description: str, layout: Layout, variables: list[dict]
) -> dict[str, Any]:
    """Envelope do dashboard.

    `editable: false` é deliberado: a fonte de verdade é este módulo, e um dashboard
    editável convida a mudança que a próxima aplicação apaga sem avisar.
    """
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": TAGS,
        "editable": False,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "timezone": "browser",
        "templating": {"list": variables},
        "annotations": {"list": []},
        "links": [
            {
                "type": "dashboards",
                "title": "Ops Centro",
                "tags": TAGS,
                "asDropdown": True,
                "includeVars": True,
                "keepTime": True,
            }
        ],
        "panels": layout.panels,
        "version": 0,
    }


# --- os quatro dashboards --------------------------------------------------------
def build_visao_geral() -> dict[str, Any]:
    """Saúde dos dois apps lado a lado: erro, latência e volume na mesma tela."""
    grid = Layout()

    grid.row("Taxa de erro")
    grid.timeseries(
        "Agents Platform — erro por agente (%)",
        [target(error_rate("agents_platform_agent_executions_total", ["agent"]), "{{agent}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
        description="Execuções com status=error sobre o total, por agente (§7).",
    )
    grid.timeseries(
        "File Memory — erro por tool (%)",
        [target(error_rate("context_mcp_tool_calls_total", ["tool"]), "{{tool}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
        description="Chamadas de tool MCP com status=error sobre o total, por tool (§7).",
    )

    grid.row("Latência p95")
    grid.timeseries(
        "Agents Platform — p95 de execução (s)",
        [target(quantile("agents_platform_agent_execution_duration_seconds", 0.95, ["agent"]),
                "{{agent}}")],
        unit=SEC,
    )
    grid.timeseries(
        "File Memory — p95 de tool MCP (s)",
        [target(quantile("context_mcp_tool_call_duration_seconds", 0.95, ["tool"]), "{{tool}}")],
        unit=SEC,
    )

    grid.row("Volume")
    grid.timeseries(
        "Agents Platform — execuções/s",
        [target(rate_by("agents_platform_agent_executions_total", ["agent"]), "{{agent}}")],
        unit=OPS,
    )
    grid.timeseries(
        "File Memory — etapas de ingestão/s",
        [target(rate_by("context_mcp_ingestion_stage_total", ["stage"]), "{{stage}}")],
        unit=OPS,
    )

    grid.row("Pipeline de observabilidade (o observador também é observado)")
    grid.stat(
        "Alertas recebidos (janela)",
        [target(increase_by("ops_centro_alerts_received_total"))],
        description="Webhooks do Grafana Alerting aceitos pelo receiver (RF06).",
    )
    grid.stat(
        "Banco de logs no Turso",
        [target(f"max(ops_centro_logs_db_bytes{{{ENV}}})")],
        unit=BYTES,
        description="Tamanho medido pelo job de retenção (issue #9). Teto do free tier: ~9 GB.",
    )
    grid.stat(
        "Linhas em `logs`",
        [target(f"max(ops_centro_logs_rows{{{ENV}}})")],
    )
    grid.stat(
        "Linhas removidas pela retenção (janela)",
        [target(increase_by("ops_centro_log_retention_deleted_total"))],
        description="Se ficar zerado por dias, o job agendado parou de rodar.",
    )

    return dashboard(
        "ops-centro-visao-geral",
        "Ops Centro — Visão geral",
        "Saúde dos dois apps lado a lado (erro, latência, volume) + estado do próprio "
        "pipeline de observabilidade. Gerado por ops_centro.grafana.dashboards (issue #10).",
        grid,
        [var_datasource(), var_query("environment", "Ambiente", "label_values(environment)")],
    )


def build_por_tenant() -> dict[str, Any]:
    """Volume e erro por cliente — é o painel que prova o RNF05 na prática: o mesmo
    `tenant_id` cruza os dois apps."""
    grid = Layout()

    grid.row("Volume por tenant")
    grid.timeseries(
        "Execuções de agente por tenant",
        [target(rate_by("agents_platform_tenant_executions_total", ["tenant_id"], TENANT),
                "{{tenant_id}}")],
        unit=OPS,
    )
    grid.timeseries(
        "Arquivos processados por tenant",
        [target(rate_by("context_mcp_tenant_files_total", ["tenant_id"], TENANT), "{{tenant_id}}")],
        unit=OPS,
    )

    grid.row("Erros por tenant")
    grid.timeseries(
        "Erro em execuções de agente (%)",
        [target(error_rate("agents_platform_tenant_executions_total", ["tenant_id"], TENANT),
                "{{tenant_id}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
    )
    grid.timeseries(
        "Erro no processamento de arquivos (%)",
        [target(error_rate("context_mcp_tenant_files_total", ["tenant_id"], TENANT),
                "{{tenant_id}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
    )

    grid.row("Totais da janela")
    grid.table(
        "Execuções por tenant (janela)",
        [target(increase_by("agents_platform_tenant_executions_total", ["tenant_id", "status"],
                            "$__range", TENANT))],
    )
    grid.table(
        "Arquivos por tenant (janela)",
        [target(increase_by("context_mcp_tenant_files_total", ["tenant_id", "status"],
                            "$__range", TENANT))],
    )

    return dashboard(
        "ops-centro-por-tenant",
        "Ops Centro — Por tenant",
        "Volume e erro por `tenant_id`, cruzando Agents Platform e File Memory (RNF05). "
        "O mesmo filtro vale para os dois apps porque o schema comum é o mesmo (issue #3).",
        grid,
        [
            var_datasource(),
            var_query("environment", "Ambiente", "label_values(environment)"),
            var_query(
                "tenant_id",
                "Tenant",
                "label_values(agents_platform_tenant_executions_total, tenant_id)",
            ),
        ],
    )


def build_agents_platform() -> dict[str, Any]:
    """Drill-down do que o Agents Platform emite: execução, tool call e custo de LLM."""
    grid = Layout()

    grid.row("agent_execution")
    grid.timeseries(
        "Latência de execução (s)",
        [
            target(quantile("agents_platform_agent_execution_duration_seconds", 0.50), "p50", "A"),
            target(quantile("agents_platform_agent_execution_duration_seconds", 0.95), "p95", "B"),
            target(quantile("agents_platform_agent_execution_duration_seconds", 0.99), "p99", "C"),
        ],
        unit=SEC,
        description="p50/p95/p99 da §7, sobre o histograma — não sobre varredura de traces.",
    )
    grid.timeseries(
        "Execuções/s por agente e desfecho",
        [target(rate_by("agents_platform_agent_executions_total", ["agent", "status"]),
                "{{agent}} · {{status}}")],
        unit=OPS,
    )

    grid.row("mcp_tool_call")
    grid.timeseries(
        "Latência de tool (s)",
        [
            target(quantile("agents_platform_tool_call_duration_seconds", 0.50, ["tool"]),
                   "p50 {{tool}}", "A"),
            target(quantile("agents_platform_tool_call_duration_seconds", 0.95, ["tool"]),
                   "p95 {{tool}}", "B"),
        ],
        unit=SEC,
    )
    grid.timeseries(
        "Erro por tool (%)",
        [target(error_rate("agents_platform_tool_calls_total", ["tool"]), "{{tool}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
    )
    grid.barchart(
        "Chamadas por tool (janela)",
        [target(increase_by("agents_platform_tool_calls_total", ["tool"]), "{{tool}}")],
    )
    grid.barchart(
        "Erros por tool (janela)",
        [target(increase_by("agents_platform_tool_calls_total", ["tool"], "$__range",
                            'status="error"'), "{{tool}}")],
    )

    grid.row("LLM — tokens, custo e modelo")
    grid.timeseries(
        "Latência de chamada LLM (s)",
        [
            target(quantile("agents_platform_llm_call_duration_seconds", 0.50, ["model"]),
                   "p50 {{model}}", "A"),
            target(quantile("agents_platform_llm_call_duration_seconds", 0.95, ["model"]),
                   "p95 {{model}}", "B"),
        ],
        unit=SEC,
    )
    grid.timeseries(
        "Tokens/s por modelo",
        [
            target(rate_by("agents_platform_llm_tokens_input_total", ["model"]),
                   "entrada · {{model}}", "A"),
            target(rate_by("agents_platform_llm_tokens_output_total", ["model"]),
                   "saída · {{model}}", "B"),
        ],
    )
    grid.stat(
        "Custo na janela (USD)",
        [target(increase_by("agents_platform_llm_cost_usd_total"))],
        unit=CURRENCY,
        decimals=2,
        description="Só conta o que o app conseguiu calcular (cost_usd é opcional no schema).",
    )
    grid.stat(
        "Tokens de entrada (janela)",
        [target(increase_by("agents_platform_llm_tokens_input_total"))],
    )
    grid.stat(
        "Tokens de saída (janela)",
        [target(increase_by("agents_platform_llm_tokens_output_total"))],
    )
    grid.stat(
        "Erro em chamadas LLM (%)",
        [target(error_rate("agents_platform_llm_calls_total"))],
        unit=PERCENT,
        decimals=2,
    )
    grid.table(
        "Custo por modelo (janela)",
        [target(increase_by("agents_platform_llm_cost_usd_total", ["model"]))],
        unit=CURRENCY,
        w=24,
    )

    return dashboard(
        "ops-centro-agents-platform",
        "Ops Centro — Agents Platform",
        "Drill-down de `agent_execution` e `mcp_tool_call`: latência, erro, tokens, custo e "
        "modelo. Métricas do catálogo da §7 (ops_centro/metrics.py).",
        grid,
        [var_datasource(), var_query("environment", "Ambiente", "label_values(environment)")],
    )


def build_file_memory() -> dict[str, Any]:
    """Funil de ingestão por etapa + latência das queries MCP de memória."""
    grid = Layout()

    grid.row("Funil de ingestão (file_ingestion)")
    grid.barchart(
        "Etapas concluídas na janela (funil)",
        [target(increase_by("context_mcp_ingestion_stage_total", ["stage"], "$__range",
                            'status="ok"'), "{{stage}}")],
        description="Etapa a etapa: onde o funil estreita é onde os arquivos estão parando.",
        w=24,
    )
    grid.timeseries(
        "Taxa de falha por etapa (%)",
        [target(error_rate("context_mcp_ingestion_stage_total", ["stage"]), "{{stage}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
        description="Item da §7: taxa de falha na ingestão de arquivos, por etapa.",
    )
    grid.timeseries(
        "Latência p95 por etapa (s)",
        [target(quantile("context_mcp_ingestion_stage_duration_seconds", 0.95, ["stage"]),
                "{{stage}}")],
        unit=SEC,
    )

    grid.row("Queries MCP de memória (mcp_memory_query)")
    grid.timeseries(
        "Latência de query (s)",
        [
            target(quantile("context_mcp_memory_query_duration_seconds", 0.50), "p50", "A"),
            target(quantile("context_mcp_memory_query_duration_seconds", 0.95), "p95", "B"),
            target(quantile("context_mcp_memory_query_duration_seconds", 0.99), "p99", "C"),
        ],
        unit=SEC,
        description="Item da §7: latência de queries MCP do serviço de memória.",
    )
    grid.timeseries(
        "Queries/s por tipo",
        [target(rate_by("context_mcp_memory_queries_total", ["query_type"]), "{{query_type}}")],
        unit=OPS,
    )
    grid.timeseries(
        "Erro por tipo de query (%)",
        [target(error_rate("context_mcp_memory_queries_total", ["query_type"]), "{{query_type}}")],
        unit=PERCENT,
        thresholds=ERROR_THRESHOLDS,
    )
    grid.timeseries(
        "Latência p95 de tool MCP (s)",
        [target(quantile("context_mcp_tool_call_duration_seconds", 0.95, ["tool"]), "{{tool}}")],
        unit=SEC,
    )

    return dashboard(
        "ops-centro-file-memory",
        "Ops Centro — File Memory / MCP",
        "Funil de `file_ingestion` por etapa e latência de `mcp_memory_query`. "
        "Métricas do catálogo da §7 (ops_centro/metrics.py).",
        grid,
        [var_datasource(), var_query("environment", "Ambiente", "label_values(environment)")],
    )


BUILDERS = {
    "visao-geral": build_visao_geral,
    "por-tenant": build_por_tenant,
    "agents-platform": build_agents_platform,
    "file-memory": build_file_memory,
}


def build_all() -> dict[str, dict[str, Any]]:
    """`{nome do arquivo (sem .json): dashboard}`."""
    return {nome: builder() for nome, builder in BUILDERS.items()}


def render(dash: dict[str, Any]) -> str:
    """JSON estável (indentado, chaves na ordem de construção) — diff legível no PR."""
    return json.dumps(dash, ensure_ascii=False, indent=2) + "\n"


# --- arquivos no repo ------------------------------------------------------------
def write_all(directory: Path | None = None) -> list[Path]:
    directory = directory or DASHBOARDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    escritos = []
    for nome, dash in build_all().items():
        caminho = directory / f"{nome}.json"
        caminho.write_text(render(dash), encoding="utf-8")
        escritos.append(caminho)
    return escritos


def check_drift(directory: Path | None = None) -> list[str]:
    """Nomes dos dashboards cujo JSON no repo difere do gerado (vazio = em dia)."""
    directory = directory or DASHBOARDS_DIR
    divergentes = []
    for nome, dash in build_all().items():
        caminho = directory / f"{nome}.json"
        if not caminho.exists() or caminho.read_text(encoding="utf-8") != render(dash):
            divergentes.append(nome)
    return divergentes


# --- métricas citadas -------------------------------------------------------------
def referenced_metrics(dash: dict[str, Any]) -> set[str]:
    """Nomes de métrica citados nas queries de um dashboard.

    Varre o JSON inteiro (painéis e variáveis) atrás de identificadores com os prefixos
    dos apps, normalizando os sufixos de histograma. É o que o teste usa para provar que
    nenhum painel aponta para métrica fora do catálogo.
    """
    import re

    from ops_centro.conventions import METRIC_PREFIXES

    prefixos = tuple(f"{p}_" for p in METRIC_PREFIXES.values())
    texto = json.dumps(dash, ensure_ascii=False)
    encontrados = set()
    for token in re.findall(r"[a-z_][a-z0-9_]*", texto):
        if not token.startswith(prefixos):
            continue
        for sufixo in ("_bucket", "_sum", "_count"):
            if token.endswith(sufixo) and token[: -len(sufixo)] in BY_NAME:
                token = token[: -len(sufixo)]
                break
        encontrados.add(token)
    return encontrados


# --- publicação via API ------------------------------------------------------------
def _dica_permissao(resposta: httpx.Response, escopo: str) -> str:
    """Mensagem de falha da API, com o conserto junto quando é questão de permissão.

    403 aqui quase sempre significa "usei o token de leitura da Fase 1". O erro cru do
    Grafana diz o escopo que falta, mas não diz onde arrumá-lo — daí a dica.
    """
    detalhe = f"HTTP {resposta.status_code}: {resposta.text[:200]}"
    if resposta.status_code in (401, 403):
        detalhe += (
            f"\n         → o token precisa de `{escopo}`. GRAFANA_READ_TOKEN (Viewer) não "
            "publica: crie um service account com role Editor em Administration → Users "
            "and access → Service accounts, e ponha o token em GRAFANA_API_TOKEN "
            "(docs/secrets.md)."
        )
    return detalhe


class GrafanaAPI:
    """Cliente mínimo da API da instância Grafana (só o que a publicação exige)."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls) -> "GrafanaAPI":
        # GRAFANA_API_TOKEN primeiro: publicar exige `dashboards:write`, escopo que o
        # token de leitura da validação da Fase 1 não tem. O fallback existe para o caso
        # de o service account ter os dois escopos.
        token = os.environ.get("GRAFANA_API_TOKEN") or os.environ.get("GRAFANA_READ_TOKEN", "")
        return cls(os.environ.get("GRAFANA_STACK_URL", ""), token)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        # Service account token (`glsa_`) fala com a instância como Bearer.
        return {"Authorization": f"Bearer {self.token}"}

    def _post(self, path: str, payload: dict) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=httpx.Timeout(30.0),
        )

    def _get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", headers=self._headers(),
                         timeout=httpx.Timeout(30.0))

    def folder_exists(self) -> bool:
        try:
            return self._get(f"/api/folders/{FOLDER_UID}").status_code == 200
        except httpx.HTTPError:
            return False

    def ensure_folder(self) -> tuple[bool, str]:
        """Cria a pasta (uid fixo). Já existir é sucesso — a operação é idempotente."""
        resposta = self._post("/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
        if resposta.status_code in (200, 201):
            return True, f"pasta '{FOLDER_TITLE}' criada"
        if resposta.status_code in (409, 412):
            return True, f"pasta '{FOLDER_TITLE}' já existe"
        # 403 com a pasta já criada não é impedimento: publicar dentro dela exige
        # `dashboards:write`, não `folders:create`. Só bloqueia se ela não existir.
        if resposta.status_code == 403 and self.folder_exists():
            return True, f"pasta '{FOLDER_TITLE}' já existe (sem permissão para criar pastas)"
        return False, _dica_permissao(resposta, "folders:create")

    def apply(self, dash: dict[str, Any]) -> tuple[bool, str]:
        """Publica um dashboard. `overwrite` + uid fixo = idempotente.

        `id: None` é obrigatório: com um `id` de outra instância no corpo, o Grafana
        tenta atualizar um dashboard que não é este e devolve 404/412.
        """
        resposta = self._post(
            "/api/dashboards/db",
            {
                "dashboard": {**dash, "id": None},
                "folderUid": FOLDER_UID,
                "overwrite": True,
                "message": "ops-centro as-code (issue #10)",
            },
        )
        if resposta.status_code == 200:
            corpo = resposta.json()
            return True, f"{corpo.get('status', 'ok')} · versão {corpo.get('version')} · {corpo.get('url', '')}"
        return False, _dica_permissao(resposta, "dashboards:write")


def apply_all(api: GrafanaAPI | None = None) -> int:
    """Publica os quatro dashboards. Devolve o código de saída do CLI."""
    api = api or GrafanaAPI.from_env()
    if not api.configured:
        print("erro: GRAFANA_STACK_URL e GRAFANA_API_TOKEN (dashboards:write) são obrigatórios")
        print("      ver docs/secrets.md")
        return 2

    ok_pasta, detalhe = api.ensure_folder()
    print(f"[{'OK' if ok_pasta else 'FALHA'}] {detalhe}")
    if not ok_pasta:
        return 1

    falhas = 0
    for nome, dash in build_all().items():
        ok, detalhe = api.apply(dash)
        falhas += 0 if ok else 1
        print(f"[{'OK' if ok else 'FALHA'}] {nome} ({dash['uid']}): {detalhe}")
    return 1 if falhas else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboards as-code do Grafana (issue #10)")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--write", action="store_true", help="(re)gera os JSONs no repo")
    grupo.add_argument("--check", action="store_true", help="falha se os JSONs estiverem desatualizados")
    grupo.add_argument("--apply", action="store_true", help="publica no Grafana Cloud")
    args = parser.parse_args(argv)

    if args.write:
        for caminho in write_all():
            print(f"escrito: {caminho.relative_to(Path.cwd()) if caminho.is_relative_to(Path.cwd()) else caminho}")
        return 0

    if args.check:
        divergentes = check_drift()
        if divergentes:
            print("JSON desatualizado em relação ao gerador: " + ", ".join(divergentes))
            print("rode: make dashboards-build")
            return 1
        print(f"{len(BUILDERS)} dashboard(s) em dia com o gerador")
        return 0

    return apply_all()


if __name__ == "__main__":
    sys.exit(main())
