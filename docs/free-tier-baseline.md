# Baseline de consumo do free tier (issue #6)

> Risco §10 do plano: estourar o free tier do Grafana Cloud. Este arquivo é o registro
> vivo do consumo medido — e a base para calibrar o sampling (RNF02).
> **Status: baseline zero registrado em 2026-07-23** (stack `radiantfennec1578`). A medição
> de regime ainda depende de 48h de emissão contínua com os apps rodando.

## Tetos do free tier

Lidos da própria instância, não de documentação — as métricas `*_included_usage` do
datasource `grafanacloud-usage` dizem a cota vigente do stack:

| Recurso | Cota incluída | Como confirmar |
| --- | --- | --- |
| Logs (ingestão/mês) | **50 GB** (medido) | `grafanacloud_org_logs_included_usage` |
| Traces (ingestão/mês) | **50 GB** (medido) | `grafanacloud_org_traces_included_usage` |
| Perfis | 50 GB | `grafanacloud_org_profiles_included_usage` |
| Métricas (séries ativas) | 10.000 | *Billing/Usage* (sem métrica de cota exposta) |
| Retenção | 14 dias logs/traces (RNF03) | — |

## Como medir

1. **Pelo dashboard**: *Grafana Cloud → Billing/Usage* mostra séries ativas, GB de logs e
   GB de traces do período.
2. **Por query** (datasource `grafanacloud-usage`, uid `grafanacloud-usage`, incluso no
   stack). Os nomes abaixo foram conferidos contra `/api/v1/label/__name__/values` da
   instância — as variantes `*_bytes_received_total` **não existem**:

   ```promql
   grafanacloud_instance_active_series                                   # séries ativas
   grafanacloud_org_logs_usage / grafanacloud_org_logs_included_usage    # fração da cota de logs
   grafanacloud_org_traces_usage / grafanacloud_org_traces_included_usage # fração da cota de traces
   avg_over_time(grafanacloud_logs_instance_bytes_received_per_second[1h]) * 86400 / 1e9   # GB/dia
   avg_over_time(grafanacloud_traces_instance_bytes_received_per_second[1h]) * 86400 / 1e9 # GB/dia
   grafanacloud_traces_instance_spans_received_total:rate5m              # spans/s
   ```

3. **Origem do consumo por app** (para saber quem gastar menos):

   ```promql
   count by (app_name) ({app_name!=""})                       # séries por app (Prometheus)
   ```
   ```logql
   # No Loki, app_name é structured metadata, não label: filtra, não seleciona stream.
   sum by (app_name) (count_over_time({service_name=~".+"} | app_name!="" [24h]))
   ```

## Medição

| Data | Séries ativas | Logs (GB/dia) | Traces (GB/dia) | Projeção mensal | Margem até o teto | Sampling em uso |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-07-23 | 0 (nenhuma métrica emitida) | 0,00016 | 0,00005 | ~0 GB | 100% dos 50 GB | `OTEL_TRACES_SAMPLER_ARG` não setado → 1.0 |

**Leitura do baseline zero:** o consumo medido é só o dos sinais de validação emitidos à
mão (2 traces, 2 linhas de log). Serve como marco de partida e como prova de que a
medição funciona — não como projeção. A medição que vale exige os apps rodando: refaça
após 48h de emissão contínua e preencha uma linha nova.

Anexe a saída de `make validate-json` da mesma janela — é o que amarra "os sinais chegaram"
com "custaram isto".

## Regra de decisão (RNF02)

| Projeção mensal | Ação |
| --- | --- |
| < 50% do teto | manter `OTEL_TRACES_SAMPLER_ARG=1.0` |
| 50–70% | baixar o sampling de sucesso para 0.25 em dev, 0.10 em prod |
| > 70% | 0.05 em prod + cortar logs `INFO` do Loki (mandar só o que precisa de retenção longa para o Turso — [turso-logs.md](turso-logs.md)) |
| séries ativas > 7.000 | caçar label de alta cardinalidade: nenhuma métrica pode ter `session_id`, `trace_id` ou `file_id` ([schema.md §3](schema.md#3-convenção-de-métricas)) |

Erros continuam 100% exportados em qualquer nível de sampling (filtro no processor de
export, não no sampler — [schema.md §5](schema.md#5-sampling-rnf02)).

O alerta automático de consumo do free tier é da Fase 3; até lá, a checagem é manual e
registrada aqui.
