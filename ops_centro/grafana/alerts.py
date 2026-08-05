"""Alert rules, contact point e notification policy as-code (issue #12).

Mesmo princípio dos dashboards (#10): regra clicada na UI não é reproduzível num stack
novo e não aparece em diff de PR. Aqui as regras são **geradas** a partir do catálogo de
métricas da §7 (`ops_centro.metrics`) e commitadas em `grafana/alerts/` no formato de
provisionamento do Grafana Alerting.

| Arquivo | Grupo | O que vigia |
| --- | --- | --- |
| `apps.yaml` | `ops-centro-apps` | taxa de erro, latência p95 e falha de ingestão dos dois apps (§7) |
| `free-tier.yaml` | `ops-centro-free-tier` | consumo do próprio free tier do Grafana Cloud (RNF02 / risco §10) |
| `turso-retencao.yaml` | `ops-centro-turso` | teto de storage do Turso e saúde do job de limpeza (issue #9) |
| `host.yaml` | `ops-centro-host` | loop de restart do systemd, memória e disco da EC2 (issue #28) |
| `roteamento.yaml` | — | contact point `webhook` → receiver + notification policy |

**Toda regra carrega o schema comum nas labels** (`app_name`, e `tenant_id` quando a série
tem): é isso que o enriquecimento do receiver (#14) usa para achar os logs no Turso, e o
que a notification policy usa para agrupar. Regra sem `app_name` é reprovada por
`validate_rules()` — o alerta chegaria no Hermes sem como ser correlacionado.

    uv run python -m ops_centro.grafana.alerts --write   # (re)gera grafana/alerts/
    uv run python -m ops_centro.grafana.alerts --check    # gate de deriva (roda no teste)
    uv run python -m ops_centro.grafana.alerts --apply    # publica no Grafana Cloud

Segredos **não** entram nos arquivos: a URL do receiver e o token do webhook ficam como
`${RECEIVER_WEBHOOK_URL}` / `${ALERT_WEBHOOK_TOKEN}`, resolvidos do ambiente na hora do
`--apply` (RNF06). O mesmo vale para os uids de datasource (`${DS_PROM}`, `${DS_USAGE}`),
que mudam de stack para stack.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from ops_centro.conventions import ATTR_APP_NAME, ATTR_ENVIRONMENT, ATTR_TENANT_ID
from ops_centro.grafana.dashboards import FOLDER_TITLE, FOLDER_UID, GrafanaAPI, _dica_permissao
from ops_centro.metrics import BY_NAME

ALERTS_DIR = Path(__file__).resolve().parents[2] / "grafana" / "alerts"

# Placeholders resolvidos no `--apply` (e pelo próprio Grafana, no provisionamento por
# arquivo). Datasource por variável para o mesmo YAML servir em outro stack amanhã.
DS_PROM = "${DS_PROM}"
DS_USAGE = "${DS_USAGE}"
DS_EXPR = "__expr__"

RECEIVER_NAME = "ops-centro-hermes"
RECEIVER_UID = "ops-centro-hermes-webhook"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
KNOWN_SEVERITIES = frozenset({SEVERITY_WARNING, SEVERITY_CRITICAL})

# Janela das `rate()`. 10m é o menor valor que ainda dá amostra suficiente com o volume
# baixo desta fase: com [5m] uma única execução com erro vira 100% de taxa de erro.
RATE_WINDOW = "10m"

DOCS = "https://github.com/CidLucas/ops-centro/blob/main"
RUNBOOK_ALERTAS = f"{DOCS}/docs/alertas.md"
RUNBOOK_RETENCAO = f"{DOCS}/docs/turso-retencao.md"
RUNBOOK_FREE_TIER = f"{DOCS}/docs/free-tier-baseline.md"

# Métricas de fora do catálogo da §7 que as regras podem citar: as de billing/uso do
# próprio Grafana Cloud (datasource `grafanacloud-usage`). Os nomes foram conferidos
# contra a instância — ver docs/free-tier-baseline.md §"Como medir".
USAGE_METRICS = (
    "grafanacloud_instance_active_series",
    "grafanacloud_org_logs_usage",
    "grafanacloud_org_logs_included_usage",
    "grafanacloud_org_traces_usage",
    "grafanacloud_org_traces_included_usage",
)

# Cotas do free tier (docs/free-tier-baseline.md). Séries ativas não têm métrica de cota
# exposta, então o número vem daqui; logs e traces dividem pela cota medida na instância.
FREE_TIER_ACTIVE_SERIES = 10_000
# Teto de storage do Turso: 9 GiB (docs/free-tier-baseline.md; mesmo valor de
# TURSO_DB_SIZE_LIMIT_BYTES).
TURSO_LIMIT_BYTES = 9 * 1024**3


# --- PromQL ---------------------------------------------------------------------
def _grouping(extra: Iterable[str] = ()) -> str:
    """Agrupamento padrão: o schema comum primeiro, depois as labels da métrica.

    `app_name`/`environment` no `by (...)` não são decoração: é como a label chega ao
    alerta e, dali, ao enriquecimento e ao agrupamento da notification policy.
    """
    return ", ".join((ATTR_APP_NAME, ATTR_ENVIRONMENT, *extra))


def error_rate(metric: str, by: Iterable[str] = (), window: str = RATE_WINDOW) -> str:
    """Percentual de erro de um counter com label `status` (ok|error).

    `clamp_min` no denominador evita `NaN` sem tráfego na janela — e `NaN` no alerta é
    pior que no painel: vira estado de erro da regra em vez de "está tudo quieto".
    """
    grupos = _grouping(by)
    erro = f'sum by ({grupos}) (rate({metric}{{status="error"}}[{window}]))'
    total = f"sum by ({grupos}) (rate({metric}[{window}]))"
    return f"100 * {erro} / clamp_min({total}, 1e-9)"


def quantile(metric: str, q: float, by: Iterable[str] = (), window: str = RATE_WINDOW) -> str:
    """Quantil de um histograma, preservando o schema comum no resultado."""
    grupos = ", ".join(("le", _grouping(by)))
    return f"histogram_quantile({q:.2f}, sum by ({grupos}) (rate({metric}_bucket[{window}])))"


def percent_of(numerador: str, denominador: str) -> str:
    """Fração percentual de uma cota, protegida contra cota zerada."""
    return f"100 * {numerador} / clamp_min({denominador}, 1)"


# --- modelo -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Rule:
    """Uma alert rule do Grafana Alerting.

    Toda regra é `expr` (refId A, instant) + limiar (refId B): dois nós é o suficiente
    para tudo que a Fase 3 precisa, e mantém o YAML legível — a inteligência mora na
    PromQL, não numa árvore de expressões.
    """

    uid: str
    title: str
    expr: str
    op: str  # gt | lt
    threshold: float
    duration: str  # o `for` da regra
    severity: str
    summary: str
    description: str
    component: str
    labels: dict[str, str] = field(default_factory=dict)
    datasource: str = DS_PROM
    lookback: int = 3600  # `relativeTimeRange.from`, em segundos
    no_data: str = "NoData"
    exec_err: str = "Error"

    def all_labels(self) -> dict[str, str]:
        """Labels da regra + severidade/componente. As labels vindas da série (`by (...)`)
        se somam a estas no momento do disparo."""
        return {"severity": self.severity, "component": self.component, **self.labels}

    def grouped_labels(self) -> set[str]:
        """Labels que a query traz da série (as do `by (...)`)."""
        return {
            label.strip()
            for grupo in re.findall(r"by \(([^)]*)\)", self.expr)
            for label in grupo.split(",")
            if label.strip() and label.strip() != "le"
        }

    def as_provisioning(self) -> dict[str, Any]:
        """A regra no formato de arquivo de provisionamento (`groups[].rules[]`)."""
        return {
            "uid": self.uid,
            "title": self.title,
            "condition": "B",
            "for": self.duration,
            "noDataState": self.no_data,
            "execErrState": self.exec_err,
            "annotations": {
                "summary": self.summary,
                "description": self.description,
                "runbook_url": self.runbook_url,
            },
            "labels": self.all_labels(),
            "data": [
                {
                    "refId": "A",
                    "relativeTimeRange": {"from": self.lookback, "to": 0},
                    "datasourceUid": self.datasource,
                    "model": {
                        "refId": "A",
                        "editorMode": "code",
                        "instant": True,
                        "expr": self.expr,
                    },
                },
                {
                    "refId": "B",
                    "datasourceUid": DS_EXPR,
                    "model": {
                        "refId": "B",
                        "type": "threshold",
                        "expression": "A",
                        "conditions": [
                            {"evaluator": {"type": self.op, "params": [self.threshold]}}
                        ],
                    },
                },
            ],
        }

    @property
    def runbook_url(self) -> str:
        if self.component == "turso":
            return RUNBOOK_RETENCAO
        if self.component == "free-tier":
            return RUNBOOK_FREE_TIER
        return RUNBOOK_ALERTAS


@dataclass(frozen=True, slots=True)
class RuleGroup:
    """Um arquivo de `grafana/alerts/`: um grupo de regras + o cabeçalho que o explica."""

    file: str
    name: str
    interval_seconds: int
    header: str
    rules: tuple[Rule, ...]

    @property
    def interval(self) -> str:
        return f"{self.interval_seconds // 60}m"

    def as_provisioning(self) -> dict[str, Any]:
        return {
            "apiVersion": 1,
            "groups": [
                {
                    "orgId": 1,
                    "name": self.name,
                    "folder": FOLDER_TITLE,
                    "interval": self.interval,
                    "rules": [rule.as_provisioning() for rule in self.rules],
                }
            ],
        }


# --- as regras -------------------------------------------------------------------
def build_apps() -> RuleGroup:
    """Limiares sobre as métricas da §7: taxa de erro, latência p95 e falha de ingestão.

    Os números são o ponto de partida da Fase 3, não verdade revelada — a calibração
    honesta exige as duas semanas de tráfego real que os apps ainda não têm (#5/#7). Cada
    um está anotado com o raciocínio para poder ser discutido em PR.
    """
    return RuleGroup(
        file="apps.yaml",
        name="ops-centro-apps",
        interval_seconds=60,
        header="""Alertas das métricas prioritárias da §7 — issue #12.

Um alerta por pergunta do plano: "os agentes estão errando?", "estão lentos?",
"a ingestão está falhando?", "algum cliente específico está sofrendo?".

Limiares: ponto de partida da Fase 3. Recalibrar com duas semanas de tráfego real
(o histograma dos p95 vira o piso; a taxa de erro em regime vira a média + folga).

Toda regra agrupa por `app_name`/`environment` — as labels do schema comum (RF02)
precisam chegar ao alerta para o enriquecimento do receiver (#14) achar os logs
correlacionados no Turso e para a notification policy agrupar por app e tenant.""",
        rules=(
            Rule(
                uid="ops-centro-agents-erro-execucao",
                title="Agents Platform: taxa de erro de execução acima de 5%",
                expr=error_rate("agents_platform_agent_executions_total", ["agent"]),
                op="gt",
                threshold=5,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="agents-platform",
                summary='Agente {{ $labels.agent }} com {{ printf "%.1f" $values.A.Value }}% '
                "de execuções em erro",
                description=(
                    "Execuções de agente com `status=error` sobre o total, na janela de 10m "
                    "(item da §7: taxa de erro por agente).\n"
                    "Investigação: o alerta chega com `trace_id` quando há um; comece pelos logs "
                    "correlacionados no Turso e pelo painel `ops-centro-agents-platform`.\n"
                    "5% é o limiar de partida — abaixo disso ainda é ruído de retry."
                ),
            ),
            Rule(
                uid="ops-centro-agents-erro-tool",
                title="Agents Platform: taxa de erro por tool MCP acima de 10%",
                expr=error_rate("agents_platform_tool_calls_total", ["tool"]),
                op="gt",
                threshold=10,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="agents-platform",
                summary='Tool {{ $labels.tool }} falhando em {{ printf "%.1f" $values.A.Value }}% '
                "das chamadas",
                description=(
                    "Chamadas de tool MCP com `status=error` sobre o total (item da §7: taxa de "
                    "erro por tool).\n"
                    "Tolerância maior que a de execução (10% vs 5%) porque tool call tem retry "
                    "por cima: o que chega ao usuário é a execução, não a tentativa.\n"
                    "Tool com falha recorrente é candidata à pausa automática do Hermes (RF09, "
                    "issue #17)."
                ),
            ),
            Rule(
                uid="ops-centro-agents-p95-execucao",
                title="Agents Platform: p95 de execução acima de 30s",
                expr=quantile("agents_platform_agent_execution_duration_seconds", 0.95, ["agent"]),
                op="gt",
                threshold=30,
                duration="15m",
                severity=SEVERITY_WARNING,
                component="agents-platform",
                summary='p95 do agente {{ $labels.agent }} em {{ printf "%.1f" $values.A.Value }}s',
                description=(
                    "Item da §7: latência p95 de execução de agente, medida sobre o histograma "
                    "(não sobre varredura de traces — no free tier isso é a diferença entre uma "
                    "leitura de série e um scan no Tempo).\n"
                    "`for: 15m` é deliberado: execução de agente é naturalmente longa e cheia de "
                    "cauda; alertar em 5m transformaria toda rajada em página."
                ),
            ),
            Rule(
                uid="ops-centro-agents-p95-llm",
                title="Agents Platform: p95 de chamada LLM acima de 60s",
                expr=quantile("agents_platform_llm_call_duration_seconds", 0.95, ["model"]),
                op="gt",
                threshold=60,
                duration="15m",
                severity=SEVERITY_WARNING,
                component="agents-platform",
                summary='p95 do modelo {{ $labels.model }} em {{ printf "%.1f" $values.A.Value }}s',
                description=(
                    "Item da §7: latência p50/p95/p99 de chamadas LLM, aqui na fatia p95 por "
                    "modelo.\n"
                    "Separado do p95 de execução porque a resposta é outra: p95 de LLM alto com "
                    "execução normal é degradação do provedor, não do nosso código."
                ),
            ),
            Rule(
                uid="ops-centro-file-memory-falha-ingestao",
                title="File Memory: falha de ingestão acima de 10% numa etapa",
                expr=error_rate("context_mcp_ingestion_stage_total", ["stage"]),
                op="gt",
                threshold=10,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="file-memory-mcp",
                summary="Etapa {{ $labels.stage }} da ingestão falhando em "
                '{{ printf "%.1f" $values.A.Value }}% dos arquivos',
                description=(
                    "Item da §7: taxa de falha na ingestão de arquivos, por etapa do pipeline "
                    "(RF04).\n"
                    "A label `stage` já diz onde o funil estreita — o painel do funil em "
                    "`ops-centro-file-memory` mostra as etapas anteriores na mesma janela."
                ),
            ),
            Rule(
                uid="ops-centro-file-memory-erro-tool",
                title="File Memory: taxa de erro por tool MCP acima de 10%",
                expr=error_rate("context_mcp_tool_calls_total", ["tool"]),
                op="gt",
                threshold=10,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="file-memory-mcp",
                summary='Tool {{ $labels.tool }} falhando em {{ printf "%.1f" $values.A.Value }}% '
                "das chamadas",
                description=(
                    "Chamadas de tool MCP do serviço de memória com `status=error` (§7).\n"
                    "Esta é a métrica que já está em produção hoje (`context_mcp_*` no mcp_brain), "
                    "então é a primeira regra que passa a valer de verdade."
                ),
            ),
            Rule(
                uid="ops-centro-file-memory-p95-query",
                title="File Memory: p95 de query de memória acima de 5s",
                expr=quantile("context_mcp_memory_query_duration_seconds", 0.95, ["query_type"]),
                op="gt",
                threshold=5,
                duration="15m",
                severity=SEVERITY_WARNING,
                component="file-memory-mcp",
                summary="p95 de query {{ $labels.query_type }} em "
                '{{ printf "%.1f" $values.A.Value }}s',
                description=(
                    "Item da §7: latência de queries MCP do serviço de memória.\n"
                    "Query de memória entra no caminho quente de um agente: 5s aqui viram 5s a "
                    "mais em toda execução que a consulta."
                ),
            ),
            Rule(
                uid="ops-centro-tenant-erro",
                title="Tenant específico com mais de 20% de erro nas execuções",
                expr=error_rate("agents_platform_tenant_executions_total", [ATTR_TENANT_ID]),
                op="gt",
                threshold=20,
                duration="15m",
                severity=SEVERITY_CRITICAL,
                component="agents-platform",
                summary="Tenant {{ $labels.tenant_id }} com "
                '{{ printf "%.1f" $values.A.Value }}% de erro',
                description=(
                    "Erro concentrado num cliente é o caso que a média global esconde: 20% para "
                    "um tenant pode ser 1% no agregado.\n"
                    "Crítico por isso, e não pelo volume — é o alerta que responde 'quem está "
                    "sofrendo' (§7: volume por tenant + RNF05).\n"
                    "A label `tenant_id` chega ao Hermes e vira a chave da consulta ao Turso (#14)."
                ),
            ),
        ),
    )


def build_free_tier() -> RuleGroup:
    """Alerta de consumo do próprio free tier (RNF02 / risco §10).

    O consumo das cotas de logs e traces é lido do datasource `grafanacloud-usage`, que
    expõe uso **e** cota do stack — dividir um pelo outro é mais honesto que chumbar
    "50 GB" numa regra, porque a cota muda com o plano.
    """
    return RuleGroup(
        file="free-tier.yaml",
        name="ops-centro-free-tier",
        interval_seconds=300,
        header="""Consumo do próprio free tier do Grafana Cloud — issue #12 (RNF02, risco §10).

O risco número um do plano é a observabilidade estourar o orçamento que ela existe para
proteger (RNF01: R$ 0–50). Estas regras são o alarme de fumaça do próprio pipeline.

Fonte: datasource `grafanacloud-usage` (uid `grafanacloud-usage`), não o Prometheus do
stack — nomes conferidos contra a instância em docs/free-tier-baseline.md.

Os limiares são os da tabela de decisão do RNF02 (free-tier-baseline.md §"Regra de
decisão"): 70% da cota manda baixar o sampling; 7.000 séries ativas mandam caçar label de
alta cardinalidade. Alerta que dispara sem ação associada é ruído — cada um destes tem a
sua na descrição.""",
        rules=(
            Rule(
                uid="ops-centro-free-tier-logs-70",
                title="Free tier: ingestão de logs acima de 70% da cota",
                expr=percent_of("grafanacloud_org_logs_usage", "grafanacloud_org_logs_included_usage"),
                op="gt",
                threshold=70,
                duration="1h",
                severity=SEVERITY_WARNING,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary='Logs em {{ printf "%.0f" $values.A.Value }}% da cota do mês',
                description=(
                    "Ação (RNF02): cortar `INFO` do Loki e mandar para o Turso só o que precisa "
                    "de retenção longa (docs/turso-logs.md → 'O que logar').\n"
                    "`for: 1h` porque a métrica de uso é acumulada no ciclo de billing: um pico "
                    "instantâneo não existe aqui, e o que importa é a tendência."
                ),
            ),
            Rule(
                uid="ops-centro-free-tier-logs-90",
                title="Free tier: ingestão de logs acima de 90% da cota",
                expr=percent_of("grafanacloud_org_logs_usage", "grafanacloud_org_logs_included_usage"),
                op="gt",
                threshold=90,
                duration="15m",
                severity=SEVERITY_CRITICAL,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary='Logs em {{ printf "%.0f" $values.A.Value }}% da cota — estouro iminente',
                description=(
                    "Passar da cota tira o stack do free tier no meio do ciclo (RNF01).\n"
                    "Ação imediata: desligar o export de logs OTLP dos apps menos críticos; a "
                    "retenção longa continua no Turso, que tem cota independente."
                ),
            ),
            Rule(
                uid="ops-centro-free-tier-traces-70",
                title="Free tier: ingestão de traces acima de 70% da cota",
                expr=percent_of(
                    "grafanacloud_org_traces_usage", "grafanacloud_org_traces_included_usage"
                ),
                op="gt",
                threshold=70,
                duration="1h",
                severity=SEVERITY_WARNING,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary='Traces em {{ printf "%.0f" $values.A.Value }}% da cota do mês',
                description=(
                    "Ação (RNF02): baixar `OTEL_TRACES_SAMPLER_ARG` — 0.25 em dev, 0.10 em prod. "
                    "Erro continua 100% exportado em qualquer nível (schema.md §5), então o "
                    "sampling não cega a investigação.\n"
                    "Trace é o sinal mais caro por byte do stack: é aqui que o corte compensa."
                ),
            ),
            Rule(
                uid="ops-centro-free-tier-traces-90",
                title="Free tier: ingestão de traces acima de 90% da cota",
                expr=percent_of(
                    "grafanacloud_org_traces_usage", "grafanacloud_org_traces_included_usage"
                ),
                op="gt",
                threshold=90,
                duration="15m",
                severity=SEVERITY_CRITICAL,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary='Traces em {{ printf "%.0f" $values.A.Value }}% da cota — estouro iminente',
                description=(
                    "Sampling agressivo agora (0.05 em prod) e revisão de quem está gerando span "
                    "em volume — o dashboard de visão geral mostra o volume por app."
                ),
            ),
            Rule(
                uid="ops-centro-free-tier-series-70",
                title="Free tier: séries ativas acima de 7.000",
                expr="max(grafanacloud_instance_active_series)",
                op="gt",
                threshold=7_000,
                duration="30m",
                severity=SEVERITY_WARNING,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary="{{ $values.A.Value }} séries ativas (cota: "
                f"{FREE_TIER_ACTIVE_SERIES})",
                description=(
                    "Séries ativas crescendo é quase sempre label de alta cardinalidade vazando "
                    "para métrica: nenhuma pode ter `session_id`, `trace_id` ou `file_id` "
                    "(schema.md §3; `ALLOWED_METRIC_LABELS` em conventions.py é a lista fechada).\n"
                    "Investigação: `count by (app_name) ({app_name!=\"\"})` no Prometheus do stack "
                    "diz qual app está gerando as séries."
                ),
            ),
            Rule(
                uid="ops-centro-free-tier-series-95",
                title="Free tier: séries ativas acima de 9.500",
                expr="max(grafanacloud_instance_active_series)",
                op="gt",
                threshold=9_500,
                duration="15m",
                severity=SEVERITY_CRITICAL,
                component="free-tier",
                datasource=DS_USAGE,
                lookback=21600,
                labels={ATTR_APP_NAME: "ops-centro"},
                summary="{{ $values.A.Value }} séries ativas — a 5% da cota de "
                f"{FREE_TIER_ACTIVE_SERIES}",
                description=(
                    "Estourar a cota de séries não custa dinheiro: **descarta métrica**. O pipeline "
                    "para de responder justamente quando mais precisa ser consultado.\n"
                    "Ação: identificar a métrica culpada e remover a label na origem — reduzir "
                    "retenção não devolve série ativa."
                ),
            ),
        ),
    )


def build_turso() -> RuleGroup:
    """Teto de storage do Turso e saúde do job de retenção (issue #9, portado para o gerador).

    As três regras cobrem os dois jeitos de a mitigação do risco §10 falhar: o banco cresce
    em direção ao teto, ou o job que o poda simplesmente parou.
    """
    crescimento = f"100 * max(ops_centro_logs_db_bytes) / {TURSO_LIMIT_BYTES}"
    return RuleGroup(
        file="turso-retencao.yaml",
        name="ops-centro-turso",
        interval_seconds=300,
        header=f"""Alertas da retenção de logs no Turso — issues #9 e #12.

As séries vêm do próprio job de limpeza (`ops_centro.turso.retention`, métricas
`ops_centro_logs_*` do catálogo em ops_centro/metrics.py). Se a regra 3 (job parado)
disparar, as regras 1 e 2 estão cegas e a investigação começa por ela.

Teto: 9 GiB = {TURSO_LIMIT_BYTES} bytes (free tier do Turso, docs/free-tier-baseline.md).
Se a cota do plano mudar, mude em TURSO_LIMIT_BYTES aqui e em TURSO_DB_SIZE_LIMIT_BYTES.""",
        rules=(
            Rule(
                uid="ops-centro-turso-db-70",
                title="Turso: banco de logs acima de 70% do free tier",
                expr=crescimento,
                op="gt",
                threshold=70,
                duration="30m",
                severity=SEVERITY_WARNING,
                component="turso",
                labels={ATTR_APP_NAME: "ops-centro"},
                lookback=21600,  # 6h: o job publica a métrica uma vez por dia
                summary='Banco de logs do Turso em {{ printf "%.0f" $values.A.Value }}% do teto',
                description=(
                    "O banco `ops-centro-logs` passou de 70% dos 9 GiB do free tier.\n"
                    "Ações, na ordem: (1) conferir se o job de retenção rodou hoje "
                    "(`ops_centro_log_retention_deleted_total`); (2) encurtar a janela por nível "
                    "em TURSO_LOG_RETENTION_DAYS; (3) rodar `make retention-vacuum` uma vez para "
                    "devolver ao disco o espaço já liberado."
                ),
            ),
            Rule(
                uid="ops-centro-turso-db-90",
                title="Turso: banco de logs acima de 90% do free tier",
                expr=crescimento,
                op="gt",
                threshold=90,
                duration="15m",
                severity=SEVERITY_CRITICAL,
                component="turso",
                labels={ATTR_APP_NAME: "ops-centro"},
                lookback=21600,
                summary='Banco de logs do Turso em {{ printf "%.0f" $values.A.Value }}% do teto',
                description=(
                    "Passar dos 9 GiB tira o banco do free tier (RNF01: custo alvo R$ 0–50).\n"
                    "A janela de retenção vigente não está compensando o volume de escrita: "
                    "encurte-a **e** revise o que os apps mandam para o Turso — INFO em volume é "
                    "papel do Loki, não da retenção longa (docs/turso-logs.md → 'O que logar')."
                ),
            ),
            Rule(
                uid="ops-centro-retencao-parada",
                title="Turso: job de retenção sem execução há 36h",
                expr="sum(increase(ops_centro_log_retention_duration_seconds_count[36h])) "
                "or vector(0)",
                op="lt",
                threshold=1,
                duration="1h",
                severity=SEVERITY_WARNING,
                component="turso",
                labels={ATTR_APP_NAME: "ops-centro"},
                lookback=129600,  # 36h
                no_data="Alerting",  # aqui a ausência de dado É o sintoma
                summary="O job de limpeza de logs não roda desde ontem",
                description=(
                    "Nenhuma amostra de `ops_centro_log_retention_duration_seconds` nas últimas "
                    "36h — o workflow `.github/workflows/retention.yml` (cron diário) falhou, foi "
                    "desabilitado, ou o secret TURSO_DATABASE_URL sumiu.\n"
                    "Sem ele o banco cresce sem limite e os alertas de tamanho ficam olhando para "
                    "uma série congelada."
                ),
            ),
        ),
    )


def build_host() -> RuleGroup:
    """Saúde do host da EC2 — a causa do incidente de 05/08/2026, não o sintoma.

    O `hermes-dashboard.service` ficou ~6 semanas (desde 23/jun) em loop de restart (800–
    1300/h) sem nenhum alerta, até exaurir memória e swap e a máquina ficar inacessível —
    só um humano descobriu. Estas regras pegam a causa (loop de restart) e os dois caminhos
    até ela (memória e disco), mais o próprio coletor (mini dead-man's switch; o #27
    completo — NoData + keep_firing + rota independente — é outra issue).
    """
    return RuleGroup(
        file="host.yaml",
        name="ops-centro-host",
        interval_seconds=60,
        header="""Alertas de host da EC2 — issue #28 (a causa do incidente de 05/08/2026).

O `hermes-dashboard.service` ficou ~6 semanas (desde 23/jun) em loop de restart de 800–
1300/h sem alerta, até exaurir memória e swap e a máquina ficar inacessível — só um humano
descobriu. Estas regras vigiam a causa (restart loop) e os dois caminhos até ela (memória
e disco), além do próprio coletor (mini dead-man's switch).

Métricas: `job="integrations/unix"` — a integração Unix do Grafana Cloud via Alloy (issue
#26). O node_exporter embutido no Alloy v1.18 não expõe `node_systemd_unit_restarts_total`,
então o loop é detectado por `changes(node_systemd_unit_state{state="failed"}[15m])`: com
`Restart=always` a unit alterna failed↔activating e `changes()` conta as transições —
robusto mesmo com scrape de 30s.""",
        rules=(
            Rule(
                uid="ops-centro-host-restart-loop",
                title="Host: unit em loop de restart (failed ≥3x/15m)",
                expr=(
                    'changes(node_systemd_unit_state{job="integrations/unix", '
                    'state="failed"}[15m])'
                ),
                op="gt",
                threshold=6,
                duration="5m",
                severity=SEVERITY_CRITICAL,
                component="ec2-host",
                labels={ATTR_APP_NAME: "ops-centro-host"},
                summary="Unit {{ $labels.name }} em loop de restart "
                "({{ printf \"%.0f\" $values.A.Value }} transições failed/15m)",
                description=(
                    "A unit `{{ $labels.name }}` está alternando failed↔activating com "
                    "`Restart=always` e `StartLimitIntervalSec=0` (o freio desligado): 800–1300 "
                    "restarts/h por semanas — foi assim que a máquina exauriu memória e swap e "
                    "ficou inacessível em 05/08/2026, depois de ~6 semanas sem alerta.\n"
                    "Investigação: `systemctl status {{ $labels.name }}`, "
                    "`journalctl -u {{ $labels.name }} --since -1h`; confira "
                    "`StartLimitBurst`/`StartLimitIntervalSec` na unit; se o processo sobe e "
                    "morre por porta ocupada, `ss -tlnp` na porta."
                ),
            ),
            Rule(
                uid="ops-centro-host-memoria",
                title="Host: memória disponível abaixo de 10% do total",
                expr=(
                    '100 * node_memory_MemAvailable_bytes{job="integrations/unix"} / '
                    'clamp_min(node_memory_MemTotal_bytes{job="integrations/unix"}, 1)'
                ),
                op="lt",
                threshold=10,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="ec2-host",
                labels={ATTR_APP_NAME: "ops-centro-host"},
                summary="Memória disponível em {{ printf \"%.0f\" $values.A.Value }}% do total",
                description=(
                    "Abaixo de 10% é thrashing iminente — o que aconteceu no incidente de "
                    "05/08/2026, quando o loop de restart exauriu memória e swap e a máquina "
                    "ficou inacessível.\n"
                    "Regime saudável hoje: ~45%. O piso `clamp_min` no denominador evita NaN "
                    "com a série zerada."
                ),
            ),
            Rule(
                uid="ops-centro-host-disco",
                title="Host: disco raiz com menos de 10% livre",
                expr=(
                    '100 * node_filesystem_avail_bytes{job="integrations/unix", '
                    'mountpoint="/"} / clamp_min(node_filesystem_size_bytes'
                    '{job="integrations/unix", mountpoint="/"}, 1)'
                ),
                op="lt",
                threshold=10,
                duration="10m",
                severity=SEVERITY_WARNING,
                component="ec2-host",
                labels={ATTR_APP_NAME: "ops-centro-host"},
                summary="Disco raiz com {{ printf \"%.0f\" $values.A.Value }}% livre",
                description=(
                    "Disco cheio é o que mata o gateway do Hermes (Errno 28) e orfana runs.\n"
                    "Regime hoje: ~18%. Para liberar: limpe `~/.hermes/logs`, `~/.cache` e "
                    "`opencode.db`."
                ),
            ),
            Rule(
                uid="ops-centro-host-coletor-parado",
                title="Host: coletor de métricas sem sinal há 10m",
                expr='up{job="integrations/unix"}',
                op="lt",
                threshold=1,
                duration="10m",
                severity=SEVERITY_CRITICAL,
                component="ec2-host",
                labels={ATTR_APP_NAME: "ops-centro-host"},
                no_data="Alerting",  # ausência de sinal É o sintoma
                summary="Coletor de métricas de host sem sinal há 10m",
                description=(
                    "O Alloy da EC2 parou de reportar: host caiu, container morreu ou o OTLP "
                    "falhou — é o alerta que o incidente de 05/08/2026 não tinha.\n"
                    "NoData vira alerta de propósito: aqui a ausência de sinal É o sintoma. "
                    "Confira o estado do Alloy e se as métricas voltaram ao Mimir."
                ),
            ),
        ),
    )


GROUP_BUILDERS = (build_apps, build_free_tier, build_turso, build_host)


def build_groups() -> list[RuleGroup]:
    return [builder() for builder in GROUP_BUILDERS]


def all_rules() -> list[Rule]:
    return [rule for grupo in build_groups() for rule in grupo.rules]


# --- roteamento: contact point + notification policy --------------------------------
ROUTING_FILE = "roteamento.yaml"

ROUTING_HEADER = """Contact point e notification policy — issue #12.

Para onde os alertas vão: o contact point `ops-centro-hermes` é o webhook do receiver
deste repo (`POST /alerts/grafana`), que enriquece com o contexto do Turso (#14) e
repassa ao Hermes/Telegram (#15).

**Autenticação (RNF06):** nenhum segredo aqui. `${ALERT_WEBHOOK_TOKEN}` e
`${RECEIVER_WEBHOOK_URL}` são resolvidos do ambiente — pelo próprio Grafana no
provisionamento por arquivo, e pelo `--apply` antes de chamar a API. O token vai nos dois
formatos que o receiver aceita: header `X-Alert-Token` (o do contrato da issue) e
`Authorization: Bearer` (que toda versão do contact point webhook suporta, mesmo as
anteriores ao suporte a headers customizados).

**Agrupamento:** por `alertname` + `app_name` + `tenant_id`. Sem isso, um incidente que
atinge 40 tenants vira 40 mensagens no Telegram — a tempestade de alertas que faz as
pessoas silenciarem o canal. Com isso, vira uma mensagem que diz quantos tenants.

> A aplicação da policy **substitui a árvore de roteamento do org** (é uma só no Grafana).
> Num stack com roteamento pré-existente, use `--skip-policy` e faça a mudança na mão."""


def build_contact_point() -> dict[str, Any]:
    """Contact point `webhook` apontando para o receiver."""
    return {
        "orgId": 1,
        "name": RECEIVER_NAME,
        "receivers": [
            {
                "uid": RECEIVER_UID,
                "type": "webhook",
                # `disableResolveMessage: false`: o "resolvido" é metade do valor de um
                # alerta — sem ele ninguém sabe quando o incidente acabou.
                "disableResolveMessage": False,
                "settings": {
                    "url": "${RECEIVER_WEBHOOK_URL}",
                    "httpMethod": "POST",
                    # Teto por notificação: o receiver enriquece alerta a alerta, e um
                    # lote gigante viraria uma consulta gigante ao Turso.
                    "maxAlerts": 20,
                    "authorization_scheme": "Bearer",
                    "authorization_credentials": "${ALERT_WEBHOOK_TOKEN}",
                    "headers": {"X-Alert-Token": "${ALERT_WEBHOOK_TOKEN}"},
                },
            }
        ],
    }


def build_policy() -> dict[str, Any]:
    """Árvore de roteamento: tudo para o receiver, agrupado pelo schema comum."""
    return {
        "orgId": 1,
        "receiver": RECEIVER_NAME,
        "group_by": ["alertname", ATTR_APP_NAME, ATTR_TENANT_ID],
        # 30s de espera junta o rebanho de alertas que nasce do mesmo incidente; 4h de
        # repetição evita que um problema conhecido vire spam a cada 5 minutos.
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": [
            {
                "receiver": RECEIVER_NAME,
                "object_matchers": [["severity", "=", SEVERITY_CRITICAL]],
                "group_wait": "10s",
                "group_interval": "5m",
                "repeat_interval": "1h",
                "continue": False,
            }
        ],
    }


def build_routing() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "contactPoints": [build_contact_point()],
        "policies": [build_policy()],
    }


# --- renderização ------------------------------------------------------------------
class _Dumper(yaml.SafeDumper):
    """Dumper com indentação de lista explícita — o YAML fica parecido com o que o
    Grafana exporta, o que facilita comparar na mão."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D102
        return super().increase_indent(flow, False)


def _str_representer(dumper: yaml.Dumper, data: str):
    """Texto com quebra de linha vira bloco literal (`|-`) em vez de string com `\\n`.

    As `description` das regras são runbook curto: precisam ser legíveis no arquivo e no
    Telegram, não uma linha de 400 caracteres.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def render(payload: dict[str, Any], header: str) -> str:
    """YAML estável (ordem de construção preservada), com o cabeçalho como comentário."""
    comentario = "\n".join(f"# {linha}".rstrip() for linha in header.splitlines())
    corpo = yaml.dump(
        payload,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        # Sem quebra automática: uma PromQL dobrada em três linhas continua válida (o YAML
        # junta com espaço), mas fica ilegível no diff e convida a erro na edição manual.
        width=4096,
    )
    return f"{comentario}\n\n{corpo}"


def build_files() -> dict[str, str]:
    """`{nome do arquivo: conteúdo}` — tudo que deve existir em `grafana/alerts/`."""
    arquivos = {
        grupo.file: render(grupo.as_provisioning(), grupo.header) for grupo in build_groups()
    }
    arquivos[ROUTING_FILE] = render(build_routing(), ROUTING_HEADER)
    return arquivos


def write_all(directory: Path | None = None) -> list[Path]:
    directory = directory or ALERTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    escritos = []
    for nome, conteudo in build_files().items():
        caminho = directory / nome
        caminho.write_text(conteudo, encoding="utf-8")
        escritos.append(caminho)
    return escritos


def check_drift(directory: Path | None = None) -> list[str]:
    """Arquivos cujo conteúdo no repo difere do gerado (vazio = em dia)."""
    directory = directory or ALERTS_DIR
    divergentes = []
    for nome, conteudo in build_files().items():
        caminho = directory / nome
        if not caminho.exists() or caminho.read_text(encoding="utf-8") != conteudo:
            divergentes.append(nome)
    return divergentes


# --- invariantes -------------------------------------------------------------------
def referenced_metrics(rule: Rule) -> set[str]:
    """Nomes de métrica citados na expressão da regra (sufixos de histograma normalizados)."""
    from ops_centro.conventions import METRIC_PREFIXES

    prefixos = tuple(f"{p}_" for p in METRIC_PREFIXES.values()) + ("grafanacloud_",)
    encontrados = set()
    for token in re.findall(r"[a-z_][a-z0-9_]*", rule.expr):
        if not token.startswith(prefixos):
            continue
        for sufixo in ("_bucket", "_sum", "_count"):
            if token.endswith(sufixo) and token[: -len(sufixo)] in BY_NAME:
                token = token[: -len(sufixo)]
                break
        encontrados.add(token)
    return encontrados


def validate_rules(rules: Iterable[Rule] | None = None) -> list[str]:
    """Regras do #12 sobre as próprias regras. Devolve a lista de violações (vazia = ok),
    no mesmo espírito de `ops_centro.metrics.validate_catalog`: relatar tudo de uma vez,
    e deixar o chamador (teste ou CLI) decidir o que fazer.
    """
    rules = list(rules if rules is not None else all_rules())
    problemas: list[str] = []
    vistos: set[str] = set()

    for rule in rules:
        if rule.uid in vistos:
            problemas.append(f"{rule.uid}: uid duplicado")
        vistos.add(rule.uid)

        if not rule.uid.startswith("ops-centro-"):
            problemas.append(f"{rule.uid}: uid deve começar com 'ops-centro-'")
        if rule.severity not in KNOWN_SEVERITIES:
            problemas.append(
                f"{rule.uid}: severity {rule.severity!r} fora de {sorted(KNOWN_SEVERITIES)}"
            )
        if not rule.duration:
            problemas.append(f"{rule.uid}: sem `for` — alerta sem histerese vira ruído")
        if not (rule.summary and rule.description):
            problemas.append(f"{rule.uid}: summary/description obrigatórios (é o corpo do aviso)")
        if rule.op not in ("gt", "lt"):
            problemas.append(f"{rule.uid}: operador {rule.op!r} não suportado (gt|lt)")

        # O schema comum precisa chegar ao alerta: ou a query o agrupa, ou a regra o fixa.
        if ATTR_APP_NAME not in rule.grouped_labels() | set(rule.all_labels()):
            problemas.append(
                f"{rule.uid}: sem `app_name` (nem no `by (...)` nem nas labels) — o alerta "
                "chegaria ao Hermes sem como ser correlacionado (#14)"
            )

        fora = sorted(
            m for m in referenced_metrics(rule) if m not in BY_NAME and m not in USAGE_METRICS
        )
        if fora:
            problemas.append(
                f"{rule.uid}: métrica fora do catálogo da §7 e da lista de uso: {', '.join(fora)}"
            )

        if "histogram_quantile" in rule.expr and not re.search(r"sum by \(le[,)]", rule.expr):
            problemas.append(f"{rule.uid}: histogram_quantile sem `le` no agrupamento devolve NaN")
        # Divisão por série (e não por constante) precisa de piso: sem tráfego na janela o
        # denominador zera, e `NaN` numa regra não vira "0%" — vira estado de erro.
        # Label value com `/` dentro de aspas (ex.: `job="integrations/unix"`) não é divisão:
        # mascarar o texto citado antes de procurar o divisor evita o falso positivo.
        sem_aspas = re.sub(r'"[^"]*"', '""', rule.expr)
        for divisor in re.findall(r"/\s*([A-Za-z_(][\w(]*)", sem_aspas):
            if not divisor.startswith("clamp_min"):
                problemas.append(
                    f"{rule.uid}: divisão por `{divisor}` sem clamp_min — zero na janela vira NaN"
                )

    return problemas


# --- publicação via API --------------------------------------------------------------
def _env_substitutions() -> dict[str, str]:
    """Valores dos `${...}` dos arquivos, vindos do ambiente (RNF06).

    Os defaults de datasource são os uids padrão de um stack do Grafana Cloud; os dois de
    segredo não têm default de propósito — `--apply` reprova sem eles.
    """
    return {
        "DS_PROM": os.environ.get("GRAFANA_PROM_DS_UID", "grafanacloud-prom"),
        "DS_USAGE": os.environ.get("GRAFANA_USAGE_DS_UID", "grafanacloud-usage"),
        "RECEIVER_WEBHOOK_URL": os.environ.get("RECEIVER_WEBHOOK_URL", ""),
        "ALERT_WEBHOOK_TOKEN": os.environ.get("ALERT_WEBHOOK_TOKEN", ""),
    }


def expand(payload: Any, valores: dict[str, str]) -> Any:
    """Substitui `${VAR}` recursivamente, em chaves e valores."""
    if isinstance(payload, dict):
        return {expand(k, valores): expand(v, valores) for k, v in payload.items()}
    if isinstance(payload, list):
        return [expand(item, valores) for item in payload]
    if isinstance(payload, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: valores.get(m.group(1), m.group(0)), payload)
    return payload


class AlertingAPI:
    """Provisioning API do Grafana Alerting (só o que a publicação exige).

    Reaproveita o cliente dos dashboards (mesma URL, mesmo token, mesma pasta): publicar
    regra e publicar painel são a mesma credencial, com escopos diferentes.
    """

    def __init__(self, api: GrafanaAPI, valores: dict[str, str] | None = None) -> None:
        self.api = api
        self.valores = valores if valores is not None else _env_substitutions()

    @classmethod
    def from_env(cls) -> "AlertingAPI":
        return cls(GrafanaAPI.from_env())

    @property
    def configured(self) -> bool:
        return self.api.configured

    def missing_secrets(self) -> list[str]:
        """Placeholders de segredo sem valor no ambiente — reprova antes de publicar
        um contact point que apontaria para lugar nenhum."""
        return [
            nome
            for nome in ("RECEIVER_WEBHOOK_URL", "ALERT_WEBHOOK_TOKEN")
            if not self.valores.get(nome)
        ]

    def apply_group(self, grupo: RuleGroup) -> tuple[bool, str]:
        """Publica um grupo inteiro (`PUT .../rule-groups/{name}`).

        O endpoint de grupo é o que torna a operação idempotente **e** convergente: o
        conteúdo do grupo passa a ser exatamente o do repo, então regra removida daqui
        também some de lá — coisa que um POST regra a regra não faz.
        """
        rules = []
        for rule in grupo.rules:
            corpo = expand(rule.as_provisioning(), self.valores)
            rules.append(
                {
                    **corpo,
                    "orgID": 1,
                    "folderUID": FOLDER_UID,
                    "ruleGroup": grupo.name,
                    "isPaused": False,
                }
            )
        resposta = self.api._put(
            f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{grupo.name}",
            {
                "title": grupo.name,
                "folderUid": FOLDER_UID,
                "interval": grupo.interval_seconds,
                "rules": rules,
            },
        )
        if resposta.status_code in (200, 202):
            return True, f"{len(rules)} regra(s) · intervalo {grupo.interval}"
        return False, _dica_permissao(resposta, "alert.rules:write")

    def apply_contact_point(self) -> tuple[bool, str]:
        """Cria ou atualiza o contact point do receiver (idempotente pelo uid)."""
        receiver = expand(build_contact_point()["receivers"][0], self.valores)
        corpo = {"name": RECEIVER_NAME, **receiver}
        resposta = self.api._put(f"/api/v1/provisioning/contact-points/{RECEIVER_UID}", corpo)
        if resposta.status_code in (200, 202):
            return True, f"contact point '{RECEIVER_NAME}' atualizado"
        # PUT em uid inexistente é 404: a primeira publicação num stack novo passa por aqui.
        if resposta.status_code == 404:
            criado = self.api._post("/api/v1/provisioning/contact-points", corpo)
            if criado.status_code in (200, 201, 202):
                return True, f"contact point '{RECEIVER_NAME}' criado"
            return False, _dica_permissao(criado, "alert.notifications:write")
        return False, _dica_permissao(resposta, "alert.notifications:write")

    def apply_policy(self) -> tuple[bool, str]:
        """Substitui a árvore de roteamento do org (o Grafana só tem uma)."""
        resposta = self.api._put("/api/v1/provisioning/policies", expand(build_policy(), self.valores))
        if resposta.status_code in (200, 202):
            return True, "notification policy aplicada (agrupa por app_name + tenant_id)"
        return False, _dica_permissao(resposta, "alert.notifications:write")


def apply_all(api: AlertingAPI | None = None, *, skip_policy: bool = False) -> int:
    """Publica grupos, contact point e policy. Devolve o código de saída do CLI."""
    api = api or AlertingAPI.from_env()
    if not api.configured:
        print("erro: GRAFANA_STACK_URL e GRAFANA_API_TOKEN são obrigatórios (docs/secrets.md)")
        return 2
    faltando = api.missing_secrets()
    if faltando:
        print(f"erro: sem valor para {', '.join(faltando)} no ambiente")
        print("      o contact point apontaria para lugar nenhum — ver docs/alertas.md")
        return 2

    ok_pasta, detalhe = api.api.ensure_folder()
    print(f"[{'OK' if ok_pasta else 'FALHA'}] {detalhe}")
    if not ok_pasta:
        return 1

    falhas = 0
    ok, detalhe = api.apply_contact_point()
    falhas += 0 if ok else 1
    print(f"[{'OK' if ok else 'FALHA'}] {RECEIVER_NAME}: {detalhe}")

    for grupo in build_groups():
        ok, detalhe = api.apply_group(grupo)
        falhas += 0 if ok else 1
        print(f"[{'OK' if ok else 'FALHA'}] grupo {grupo.name}: {detalhe}")

    if skip_policy:
        print("[SKIP] notification policy (--skip-policy)")
    else:
        ok, detalhe = api.apply_policy()
        falhas += 0 if ok else 1
        print(f"[{'OK' if ok else 'FALHA'}] roteamento: {detalhe}")

    return 1 if falhas else 0


def _print_rules() -> None:
    largura = max(len(rule.uid) for rule in all_rules())
    for grupo in build_groups():
        print(f"\n{grupo.name} ({grupo.file}, intervalo {grupo.interval})")
        for rule in grupo.rules:
            sinal = ">" if rule.op == "gt" else "<"
            print(
                f"  {rule.uid:<{largura}}  {rule.severity:<8} "
                f"{sinal} {rule.threshold:<8g} por {rule.duration:<4}  {rule.title}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alertas as-code do Grafana (issue #12)")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--write", action="store_true", help="(re)gera os YAMLs no repo")
    grupo.add_argument("--check", action="store_true", help="falha se os YAMLs divergirem")
    grupo.add_argument("--apply", action="store_true", help="publica no Grafana Cloud")
    grupo.add_argument("--list", action="store_true", help="lista as regras e seus limiares")
    parser.add_argument(
        "--skip-policy",
        action="store_true",
        help="não substituir a árvore de roteamento do org (só com --apply)",
    )
    parser.add_argument("--json", action="store_true", help="saída JSON (com --list)")
    args = parser.parse_args(argv)

    problemas = validate_rules()
    if problemas:
        for problema in problemas:
            print(f"[REGRA INVÁLIDA] {problema}")
        return 2

    if args.list:
        if args.json:
            print(json.dumps([g.as_provisioning() for g in build_groups()], ensure_ascii=False,
                             indent=2))
        else:
            _print_rules()
        return 0

    if args.write:
        for caminho in write_all():
            print(f"escrito: {caminho.name}")
        return 0

    if args.check:
        divergentes = check_drift()
        if divergentes:
            print("YAML desatualizado em relação ao gerador: " + ", ".join(divergentes))
            print("rode: make alerts")
            return 1
        print(f"{len(build_files())} arquivo(s) de alerta em dia com o gerador")
        return 0

    return apply_all(skip_policy=args.skip_policy)


if __name__ == "__main__":
    sys.exit(main())
