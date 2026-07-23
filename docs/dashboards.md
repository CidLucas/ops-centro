# Dashboards as-code (issues #10 e #11)

> Gerador: [`ops_centro/grafana/dashboards.py`](../ops_centro/grafana/dashboards.py) ·
> JSONs: [`grafana/dashboards/`](../grafana/dashboards/) ·
> catálogo de métricas: [`ops_centro/metrics.py`](../ops_centro/metrics.py).

Dashboard clicado na UI e não versionado se perde — e não é reproduzível num stack novo.
Aqui os quatro dashboards da Fase 2 são **gerados** a partir do catálogo de métricas da
§7 e commitados como JSON.

## 1. Os quatro dashboards

| uid | Arquivo | Responde |
| --- | --- | --- |
| `ops-centro-visao-geral` | [`visao-geral.json`](../grafana/dashboards/visao-geral.json) | saúde dos dois apps lado a lado (erro, latência p95, volume) + estado do próprio pipeline |
| `ops-centro-por-tenant` | [`por-tenant.json`](../grafana/dashboards/por-tenant.json) | execuções, arquivos e erros por `tenant_id`, cruzando os dois apps (RNF05) |
| `ops-centro-agents-platform` | [`agents-platform.json`](../grafana/dashboards/agents-platform.json) | drill-down de `agent_execution`/`mcp_tool_call`: latência p50/p95/p99, tokens, custo, modelo |
| `ops-centro-file-memory` | [`file-memory.json`](../grafana/dashboards/file-memory.json) | funil de `file_ingestion` por etapa + latência de `mcp_memory_query` |

Todos têm variável `environment` (multi + All) e datasource parametrizado (`DS_PROM`), e
todos são `editable: false` — a fonte de verdade é o gerador, e dashboard editável convida
a mudança que a próxima aplicação apaga sem avisar.

## 2. Fluxo de trabalho

```bash
make dashboards         # (re)gera os JSONs a partir do gerador
make dashboards-check   # falha se os JSONs commitados divergirem (mesmo gate do teste)
make dashboards-apply   # publica no Grafana Cloud
```

Mudar um painel = mudar `ops_centro/grafana/dashboards.py` → `make dashboards` →
commitar o JSON junto. O teste `tests/test_dashboards.py` reprova PR com JSON defasado,
que é o jeito clássico de as duas fontes divergirem.

**Publicar é idempotente:** cada dashboard tem uid fixo e vai com `overwrite`, na pasta
`Ops Centro`. Rodar duas vezes não cria cópia; rodar num stack zerado reconstrói tudo
(critério de aceite do #10).

Credencial: `GRAFANA_STACK_URL` + `GRAFANA_API_TOKEN` (escopo `dashboards:write`). O
`GRAFANA_READ_TOKEN` da validação da Fase 1 **não** publica — ver [secrets.md](secrets.md).

## 3. Por que gerar em vez de exportar da UI

O gerador monta cada query a partir do catálogo, e isso dá duas garantias que um export
não dá:

1. **Nenhum painel aponta para métrica que não existe.** Painel com nome de métrica errado
   não dá erro: dá gráfico vazio, que é indistinguível de "não houve tráfego". O teste
   compara todos os nomes citados contra o catálogo.
2. **Nenhuma query usa label fora do schema.** Cardinalidade é o risco número um do free
   tier (§10) — `session_id`/`trace_id`/`file_id` em label são proibidos, e o painel que
   os usasse denunciaria uma métrica errada na origem.

Detalhes de PromQL que ficam centralizados no gerador (e portanto valem para todos os
painéis de uma vez):

- taxa de erro sempre com `clamp_min(..., 1e-9)` no denominador — sem isso o painel
  **some** em vez de mostrar 0% quando não houve tráfego na janela;
- `histogram_quantile` sempre com `le` no `by (...)`, senão o resultado é NaN;
- `$__rate_interval` (e não `[5m]` fixo) para o gráfico continuar correto em qualquer zoom.

## 4. Estado da emissão

Os painéis existem antes de todas as métricas existirem — de propósito: o dashboard é o
contrato que a instrumentação dos apps tem que cumprir. Hoje só as quatro métricas
`context_mcp_*` do `mcp_brain` estão em produção; o resto depende das issues #5 e #7 nos
repos dos apps. Painel de métrica ainda não emitida aparece vazio, e

```bash
make metrics-check      # ou: uv run python -m ops_centro.metrics --check
```

diz exatamente qual está faltando, métrica por métrica, direto do Prometheus do stack —
ver [metricas-prioritarias.md](metricas-prioritarias.md).
