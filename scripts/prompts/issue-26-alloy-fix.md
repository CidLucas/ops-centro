# Issue #26 — Corrigir config.alloy do Alloy (bloco retry_on_failure)

## Contexto

O repo é `CidLucas/ops-centro` (clone em `/tmp/ops-centro`). Estamos adicionando
métricas de host da EC2 via Grafana Alloy (issue #26). O arquivo
`deploy/alloy/config.alloy` define o pipeline:

```
prometheus.exporter.unix "host"  →  prometheus.scrape "host"  →  otelcol.receiver.prometheus "host"  →  otelcol.exporter.otlphttp "grafana_cloud"
```

O config atual foi validado por `docker run --rm grafana/alloy:v1.18.0 fmt`, mas o
Alloy rejeita o config ao rodar com este erro:

```
/etc/alloy/config.alloy:51:3: unrecognized block name "retry"
```

## Causa raiz (verificada na doc oficial)

No componente `otelcol.exporter.otlphttp` do Alloy, o bloco de retry NÃO se chama
`retry` — chama-se `retry_on_failure` (e o de fila é `sending_queue`). Referência:
https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.exporter.otlphttp/

O arquivo atual tem um bloco `retry { ... }` como filho do exporter (fora do
`client`), com os campos `enabled`, `initial_interval`, `max_interval` — esses campos
estão corretos, só o NOME do bloco está errado.

## Tarefa

Edite **apenas** `deploy/alloy/config.alloy`:

1. Renomeie o bloco `retry { ... }` para `retry_on_failure { ... }`, mantendo os
   campos `enabled = true`, `initial_interval = "5s"`, `max_interval = "30s"`.
2. Não altere mais nada: o pipeline (exporter.unix → scrape → receiver → otlphttp),
   os `set_collectors`, o `scrape_interval = "30s"`, o `client { endpoint, headers }`
   — tudo isso está correto e validado.
3. Confira na doc oficial (link acima) a sintaxe exata do bloco `retry_on_failure`
   (campos: `enabled`, `initial_interval`, `max_interval`).

## Critérios de aceite

1. `docker run --rm -v /tmp/ops-centro/deploy/alloy:/etc/alloy grafana/alloy:v1.18.0 fmt /etc/alloy/config.alloy` retorna exit 0 (sintaxe válida).
2. `docker run --rm -v /tmp/ops-centro/deploy/alloy:/etc/alloy grafana/alloy:v1.18.0 run --server.http.listen-addr=127.0.0.1:0 --stability.level=public-preview /etc/alloy/config.alloy` inicia sem erro de configuração (pode rodar 3-5s e ser encerrado; o que importa é NÃO aparecer "unrecognized block" nem "could not perform the initial load").
3. Não modifique nenhum outro arquivo.
