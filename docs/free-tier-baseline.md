# Baseline de consumo do free tier (issue #6)

> Risco §10 do plano: estourar o free tier do Grafana Cloud. Este arquivo é o registro
> vivo do consumo medido — e a base para calibrar o sampling (RNF02).
> **Status: aguardando 48h de emissão contínua com os dois apps instrumentados.**

## Tetos do free tier (Grafana Cloud, 2026)

| Recurso | Teto free | Retenção |
| --- | --- | --- |
| Métricas (séries ativas) | 10.000 | 13 meses |
| Logs (ingestão/mês) | 50 GB | 14 dias (RNF03) |
| Traces (ingestão/mês) | 50 GB | 14 dias |
| Perfis | 50 GB | — |
| Usuários | 3 | — |

Confirme os números vigentes em *Billing/Usage* antes de tirar conclusões — o free tier muda.

## Como medir

1. **Pelo dashboard**: *Grafana Cloud → Billing/Usage* mostra séries ativas, GB de logs e
   GB de traces do período.
2. **Por query** (datasource `grafanacloud-<org>-usage`, incluso no stack):

   ```promql
   grafanacloud_instance_active_series                       # séries ativas
   sum(rate(grafanacloud_logs_instance_bytes_received_total[24h])) * 86400 / 1e9   # GB/dia de logs
   sum(rate(grafanacloud_traces_instance_bytes_received_total[24h])) * 86400 / 1e9 # GB/dia de traces
   ```

3. **Origem do consumo por app** (para saber quem gastar menos):

   ```promql
   count by (app_name) ({app_name!=""})                       # séries por app
   sum by (app_name) (count_over_time({app_name!=""}[24h]))   # linhas de log por app (Loki)
   ```

## Medição

| Data | Séries ativas | Logs (GB/dia) | Traces (GB/dia) | Projeção mensal | Margem até o teto | Sampling em uso |
| --- | --- | --- | --- | --- | --- | --- |
| _(preencher após 48h)_ | | | | | | `OTEL_TRACES_SAMPLER_ARG=1.0` (dev) |

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
