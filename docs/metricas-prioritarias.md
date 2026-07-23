# Métricas prioritárias da §7 (issue #11)

> Fonte de verdade executável: [`ops_centro/metrics.py`](../ops_centro/metrics.py) ·
> painéis: [dashboards.md](dashboards.md) · schema comum: [schema.md](schema.md).

A §7 do plano lista o conjunto mínimo de métricas da fase inicial. Esta página fecha o vão
entre *"o trace existe"* e *"a métrica agregada é consultável barato"*: responder "qual a
taxa de erro da tool X na última hora?" varrendo traces no Tempo custa muito mais, no free
tier, do que ler uma série no Mimir.

Cada item da §7 vira aqui um instrumento nomeado, com tipo, unidade e **labels fechadas**.
O catálogo é o mesmo objeto que gera os painéis e que o checker consulta — três lados,
uma lista.

## 1. O catálogo

```bash
make metrics          # lista o catálogo por item da §7
make metrics-check    # consulta o Prometheus do stack, métrica por métrica
```

### Agents Platform (`agents_platform_*`) — instrumentação no `repo_platform` (#5)

| Métrica | Tipo | Labels | Item da §7 |
| --- | --- | --- | --- |
| `agents_platform_agent_executions_total` | counter | `agent`, `status` | taxa de erro por agente |
| `agents_platform_agent_execution_duration_seconds` | histogram | `agent` | latência |
| `agents_platform_tool_calls_total` | counter | `tool`, `status` | taxa de erro por tool |
| `agents_platform_tool_call_duration_seconds` | histogram | `tool` | latência de MCP tools |
| `agents_platform_llm_calls_total` | counter | `model`, `status` | taxa de erro |
| `agents_platform_llm_call_duration_seconds` | histogram | `model` | latência de chamadas LLM |
| `agents_platform_llm_tokens_input_total` | counter | `model` | drill-down de custo |
| `agents_platform_llm_tokens_output_total` | counter | `model` | idem |
| `agents_platform_llm_cost_usd_total` | counter | `model` | idem |
| `agents_platform_tenant_executions_total` | counter | `tenant_id`, `status` | volume por tenant |

### File Memory / MCP (`context_mcp_*`) — instrumentação no `mcp_brain` (#7)

| Métrica | Tipo | Labels | Item da §7 | Estado |
| --- | --- | --- | --- | --- |
| `context_mcp_tool_calls_total` | counter | `tool`, `status` | taxa de erro por tool | emitindo |
| `context_mcp_tool_call_duration_seconds` | histogram | `tool` | latência | emitindo |
| `context_mcp_ingestion_stage_total` | counter | `stage`, `status` | falha na ingestão | emitindo |
| `context_mcp_ingestion_stage_duration_seconds` | histogram | `stage` | falha/latência por etapa | emitindo |
| `context_mcp_memory_queries_total` | counter | `query_type`, `status` | queries MCP | pendente |
| `context_mcp_memory_query_duration_seconds` | histogram | `query_type` | latência de query MCP | pendente |
| `context_mcp_tenant_files_total` | counter | `tenant_id`, `status` | volume por tenant | pendente |

### Ops Centro (`ops_centro_*`) — este repo

| Métrica | Emitida por |
| --- | --- |
| `ops_centro_alerts_received_total{status}` | receiver, a cada webhook aceito (RF06) |
| `ops_centro_alert_enrichment_total{status}` | receiver, a cada consulta de contexto no Turso ([#14](alertas.md#4-enriquecimento-issue-14)) — `error` = alerta saiu sem contexto |
| `ops_centro_alert_enrichment_duration_seconds` | idem: encostar no deadline é o aviso de que os alertas vão começar a chegar pelados |
| `ops_centro_log_retention_deleted_total{level}` | job de [retenção](turso-retencao.md) |
| `ops_centro_log_retention_duration_seconds` | idem |
| `ops_centro_logs_rows` / `ops_centro_logs_db_bytes` | idem |

Não são da §7: existem porque o observador também é observado. No counter de alertas o
label `status` segue o vocabulário congelado `ok`|`error` (`firing` → `error`,
`resolved` → `ok`) em vez de ecoar o payload do Grafana — métrica é observação de saúde,
não cópia do evento.

## 2. Cardinalidade — as regras que o catálogo impõe

`validate_catalog()` reprova, e o teste roda sobre o catálogo inteiro:

- **Label fora do vocabulário** de [`ALLOWED_METRIC_LABELS`](../ops_centro/conventions.py)
  é erro. `session_id`, `trace_id` e `file_id` nunca entram — são exatamente os campos de
  alta cardinalidade que fariam as 10.000 séries ativas do free tier estourarem.
- **`tenant_id` só em métrica de volume/uso.** Daí `agents_platform_tenant_executions_total`
  existir separado de `agent_executions_total`: multiplicar `tenant × agent × status` é o
  jeito mais rápido de explodir a contagem de séries. Duas métricas de baixa dimensão
  respondem as mesmas perguntas que uma de alta.
- **Sufixos e unidades:** counter termina em `_total`; latência em `_duration_seconds`
  com unit `s` (nada de `_ms`); tamanho em `_bytes`.
- **Tokens de entrada e saída em counters separados**, em vez de uma label `kind`: dobrar
  a série não responde nada que os dois nomes já não respondam.

Duas labels entraram no schema com esta issue (v1.1, ambas enumeráveis): `query_type`,
para as queries MCP de memória, e `level`, para o counter de linhas removidas pela
retenção.

## 3. O que `make metrics-check` verifica

Para cada métrica do catálogo, roda no Prometheus/Mimir:

```promql
count by (app_name, environment, <labels da métrica>) (<série>)
```

Um `count(...)` puro esconderia label faltando; o `count by` responde as duas perguntas de
uma vez — a série existe **e** carrega os labels comuns do RF02. O relatório imprime a
query, para colar no Explore (mesma disciplina auditável do [gate da Fase 1](validacao-fase1.md)).

Leitura da saída:

- **PASS** — série presente com todos os labels esperados.
- **FALHA** — o app deveria estar emitindo e não está, ou está emitindo sem algum label
  comum. É deriva real entre catálogo e realidade.
- **SKIP** — instrumentação ainda pendente no repo do app (#5/#7). Backlog conhecido não
  vira falha vermelha sem informação.

Credenciais são as mesmas do `make validate` (`GRAFANA_READ_TOKEN` + `GRAFANA_STACK_URL`) —
[secrets.md](secrets.md).

## 4. RF02 em métricas: atributo do **ponto**, não do Resource ⚠️

Descoberto ao rodar o gate contra o stack real em 2026-07-23, e vale para os dois apps:

**A ingestão OTLP do Grafana Cloud não promove resource attribute a label de métrica.**
Uma métrica emitida com `app_name`/`environment` só no Resource chega ao Mimir assim:

```promql
ops_centro_logs_rows{job="ops-centro", service_name="ops-centro"}   # e nada mais
```

Sem `app_name`, sem `environment` e sem `target_info` para fazer join — ou seja, invisível
para qualquer query cruzada por app/ambiente, que é o ponto do RNF05. Em **traces** o
Resource sobrevive (`resource.app_name` no Tempo) e em **logs** vira structured metadata;
em métricas, não.

A saída é emitir os dois como **atributos do ponto**, que viram label sempre:

```python
from ops_centro.metrics import common_labels          # {"app_name": ..., "environment": ...}

tool_calls.add(1, {**common_labels(APP_FILE_MEMORY), "tool": nome, "status": "ok"})
latencia.record(duracao, {**common_labels(APP_FILE_MEMORY), "tool": nome})
```

Custo: duas labels enumeráveis por série — as mesmas que o schema já permite. `version`
fica de fora de propósito: mudaria a cada deploy e criaria série nova a cada release.

> **Pendência cross-repo:** as quatro métricas `context_mcp_*` já em produção no
> `mcp_brain` emitem sem esses atributos de ponto — aparecem no Mimir, mas reprovam no
> `make metrics-check`. Rastreado em [mcp_brain#28](https://github.com/CidLucas/mcp_brain/issues/28),
> com o conserto na **lib compartilhada** e não no app: um app não deveria precisar saber
> que o Grafana Cloud trata Resource diferente por tipo de sinal (o `record_metric` do
> `blu_observability_bootstrap` tem o mesmo defeito).

Detalhe que torna o bug fácil de não ver localmente: pelo `PrometheusMetricReader` (scrape
do `/metrics`) o Resource vira `target_info` e o join funciona. É só o caminho OTLP → Grafana
Cloud que perde os atributos — o mesmo código está certo num destino e errado no outro.

Também vale para counters: emita `0` explicitamente nos casos sem ocorrência. Counter que
nunca incrementou não cria série, e aí o painel mostra *"No data"* onde o certo é `0` —
indistinguível de "o job não rodou", que é exatamente o que o alerta
`ops-centro-retencao-parada` precisa diferenciar.

## 5. Adicionando uma métrica

1. Entrada nova em `CATALOG` (`ops_centro/metrics.py`), com item da §7 e labels.
2. `make test` — a validação do catálogo e os testes de dashboard rodam juntos.
3. Painel: acrescente ao dashboard correspondente em `ops_centro/grafana/dashboards.py`,
   rode `make dashboards` e commite o JSON.
4. Instrumente no repo do app com **exatamente** o mesmo nome e labels (os apps replicam
   as constantes — [schema.md §4](schema.md#4-mecanismo-de-compartilhamento-decisão)).
