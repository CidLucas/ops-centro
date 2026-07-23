# Roteiro de validação da Fase 1 (issue #6)

> Critério de saída da Fase 1 (§9 do plano): sinais do app piloto visíveis no Grafana Cloud,
> com os atributos do RF02 consultáveis e cruzáveis entre apps (RNF05).
> Automação: [`ops_centro/validation.py`](../ops_centro/validation.py) · `make validate`.

## 0. Pré-requisitos

| Item | Estado |
| --- | --- |
| Stack Grafana Cloud + secrets OTLP (#2) | ✅ `stack-1733152-otel-dev`, `prod-sa-east-1` |
| Atributos comuns + sampling na lib (#4) | ✅ `blu_observability_bootstrap` 0.3.0 |
| agents-platform instrumentado (#5) | ⏳ pré-requisito deste roteiro |
| file-memory-mcp instrumentado (#7) | ✅ spans `file_ingestion` / `mcp_memory_query` |
| **Token de LEITURA do Grafana Cloud** | ⏳ ver §1 |

## 1. Credenciais de leitura (passo manual, uma vez)

O token OTLP atual é **write-only** (`metrics:write`, `logs:write`, `traces:write`):
consultar com ele devolve vazio/403. Há dois caminhos de leitura, e o validador escolhe
sozinho pelo prefixo do token.

**a) Service account da instância (`glsa_...`) — mais simples.** Em *Grafana → Administration
→ Users and access → Service accounts*, crie uma conta com role `Viewer` e um token. As
queries vão pelo proxy da instância, então basta a URL do stack:

```bash
GRAFANA_READ_TOKEN=glsa_...
GRAFANA_STACK_URL=https://<slug>.grafana.net
```

**b) Access Policy (`glc_...`) — acesso direto aos datasources.** Em *Grafana Cloud →
Access Policies*, crie uma policy com `metrics:read`, `logs:read`, `traces:read`; pegue URLs
e user IDs em *Stack → Details* (Prometheus, Loki e Tempo têm IDs **diferentes** do
`1733152` do gateway OTLP):

```bash
GRAFANA_READ_TOKEN=glc_...
GRAFANA_PROM_URL=https://prometheus-prod-<n>-prod-sa-east-1.grafana.net/api/prom
GRAFANA_PROM_USER=<id do Prometheus>
GRAFANA_LOKI_URL=https://logs-prod-<n>.grafana.net
GRAFANA_LOKI_USER=<id do Loki>
GRAFANA_TEMPO_URL=https://tempo-prod-<n>-prod-sa-east-1.grafana.net
GRAFANA_TEMPO_USER=<id do Tempo>
```

> **Instância adormecida:** no free tier, um stack sem acesso recente responde
> `503 {"code":"Loading"}` em **toda** a API, inclusive com token válido. Não dá para
> acordar por API — abra `https://<slug>.grafana.net` no navegador e clique no aviso.

## 2. Disparar tráfego no dev

| App | Como gerar sinal | O que tem que aparecer |
| --- | --- | --- |
| agents-platform | executar um agente com pelo menos uma tool call | trace `agent_execution` com filhos `mcp_tool_call` |
| file-memory-mcp | ingerir um arquivo (`ingest_document`) e fazer uma busca | trace `file_ingestion` com filhos por estágio + span `mcp_memory_query` |
| ops-centro | `curl -H "X-Alert-Token: ..." -d @alerta.json localhost:8080/alerts/grafana` | métrica/log do receiver |

Espere ~1 min (intervalo de export de métricas: `OTEL_METRIC_EXPORT_INTERVAL`, default 60s).

## 3. Checklist

```bash
make validate            # roda tudo e imprime as queries
make validate-json       # mesma coisa em JSON (para colar no baseline)
```

O comando executa, por app:

- [ ] **Métricas** — existem séries com o prefixo do app (`agents_platform_`, `context_mcp_`,
      `ops_centro_`) e toda série carrega `app_name` + `environment` + `version`.
- [ ] **Logs** — `sum(count_over_time({app_name="<app>"}[1h]))` > 0 no Loki.
- [ ] **Traces** — a busca `{resource.app_name="<app>"}` devolve traces no Tempo **e**
      100% deles também casam com `environment!=nil && version!=nil` (critério de aceite do #3).
- [ ] **Cruzamento (RNF05)** — `{span.tenant_id!=nil}` devolve traces de **mais de um**
      serviço, provando a query cruzada por tenant entre os apps.

Saída: `PASS` / `FALHA` / `SKIP` por checagem, sempre com a query usada — cole no *Explore*
para conferir na mão. Exit code 1 só quando há FALHA (SKIP = gate não executado, não reprovado).

### Conferências manuais que o script não cobre

- [ ] Abrir um trace do agents-platform no Tempo e confirmar que os `mcp_tool_call`
      aparecem como **filhos** do `agent_execution` (RF03), não como traces soltos.
- [ ] Abrir um trace do file-memory-mcp e confirmar as etapas do pipeline como spans
      filhos, com `file_id`/`file_type`/`file_size_bytes` no span pai (RF04).
- [ ] Em um log do Loki, usar *Derived fields* / `trace_id` para pular do log para o trace.

## 4. Baseline de consumo

Depois de ~48h de emissão contínua, registre o consumo em
[free-tier-baseline.md](free-tier-baseline.md) e ajuste o sampling (RNF02) se a projeção
mensal passar de ~70% de qualquer teto.

## 5. Registro da execução

| Data | Executado por | Resultado | Observações |
| --- | --- | --- | --- |
| _(preencher)_ | | | |
