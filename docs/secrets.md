# Mapa de secrets (RNF06)

Nenhum valor vive em código — só nomes e onde cada um é usado. Valores ficam em
`.env` local (gitignorado), GitHub Actions secrets e no `.env` da EC2 do Hermes.

## Grafana Cloud

Stack: `stack-1733152-otel-dev` · org `1853471` · região `prod-sa-east-1` (São Paulo).
Gateway OTLP: `https://otlp-gateway-prod-sa-east-1.grafana.net/otlp` (auth Basic `1733152:<token glc_...>` — o `1733152` é o ID do gateway, NÃO o org id do payload do token).

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL do gateway OTLP | `blu_observability_bootstrap` nos 3 apps | Secrets de `ops-centro`, `mcp_brain`, `repo_platform` + `.env` local/EC2 |
| `OTEL_EXPORTER_OTLP_HEADERS` | `Authorization=Basic <base64(1733152:token)>` — **com espaço literal, NUNCA URL-encoded** (`%20` quebra o Alloy, que não decodifica; o SDK Python decodifica sozinho) | idem | idem |
| `OTEL_EXPORTER_OTLP_AUTH` | **Só o valor** `Basic <base64(1733152:token)>` (sem o prefixo `Authorization=`), para o Alloy — o Alloy usa o valor do env como header HTTP inteiro, não faz parse de `k=v` | Alloy no compose (`deploy/alloy/config.alloy`) | `.env` da EC2 |

Rotação: gerar novo token em Grafana Cloud → Access Policies, atualizar os
secrets nos 3 repos (`gh secret set`) e os `.env` locais/EC2. O token atual é de
**dev**; criar token separado para prod quando houver ambiente prod.

## Grafana Cloud — leitura (issue #6)

O token OTLP acima é **write-only**; validar os sinais e medir o baseline exige uma
Access Policy separada com `metrics:read`, `logs:read`, `traces:read`.

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `GRAFANA_READ_TOKEN` | `glsa_...` (service account) ou `glc_...` (policy de leitura) | `ops_centro.validation` (`make validate`) | `.env` local |
| `GRAFANA_STACK_URL` | `https://<slug>.grafana.net` (só no modo `glsa_`) | idem | idem |
| `GRAFANA_PROM_URL` / `GRAFANA_PROM_USER` | URL + id do datasource Prometheus | idem | idem |
| `GRAFANA_LOKI_URL` / `GRAFANA_LOKI_USER` | URL + id do datasource Loki | idem | idem |
| `GRAFANA_TEMPO_URL` / `GRAFANA_TEMPO_USER` | URL + id do datasource Tempo | idem | idem |

Os user IDs dos datasources são diferentes do `1733152` do gateway OTLP (Stack → Details).

## Grafana Cloud — escrita de dashboards (issue #10)

Publicar dashboards as-code exige `dashboards:write`, escopo que o token de leitura acima
não tem. Use um service account com a role *Editor* (ou uma Access Policy com
`dashboards:write`) e o mesmo `GRAFANA_STACK_URL`.

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `GRAFANA_API_TOKEN` | `glsa_...` com `dashboards:write` | `ops_centro.grafana.dashboards` (`make dashboards-apply`) | `.env` local |

Sem ele, `make dashboards` e `make dashboards-check` continuam funcionando (são offline);
só a publicação exige credencial.

## Grafana Cloud — alertas as-code (issue #12)

Mesmo `GRAFANA_API_TOKEN` dos dashboards, com dois escopos a mais: `alert.rules:write` e
`alert.notifications:write` (a role *Editor* já cobre os quatro). Ver [alertas.md](alertas.md).

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `RECEIVER_WEBHOOK_URL` | `https://<domínio>/alerts/grafana` | `ops_centro.grafana.alerts` (`make alerts-apply`) | `.env` local (não é segredo, mas muda por ambiente) |
| `GRAFANA_PROM_DS_UID` / `GRAFANA_USAGE_DS_UID` | uid do datasource | idem | `.env` local (defaults `grafanacloud-prom` / `grafanacloud-usage`) |

Os YAMLs em `grafana/alerts/` carregam `${ALERT_WEBHOOK_TOKEN}` e `${RECEIVER_WEBHOOK_URL}`
como placeholder — o valor só existe no ambiente de quem publica. É o que permite commitar
o contact point sem commitar o token.

## Receiver / Hermes

| Variável | Quem usa | Onde está |
| --- | --- | --- |
| `ALERT_WEBHOOK_TOKEN` | Receiver (valida `X-Alert-Token` ou `Authorization: Bearer`) e contact point do Grafana | SSM `/ops-centro/prod/` → `.env` da EC2 + `--apply` do contact point (issue #12) |
| `ALERT_ENRICHMENT_TIMEOUT` | Receiver (deadline da consulta ao Turso, default 2s) | `.env` EC2 — não é segredo |
| `GRAFANA_TEMPO_DS_UID` | Receiver (link do trace na mensagem enriquecida) | `.env` EC2 (default `grafanacloud-traces`) |
| `GRAFANA_READ_TOKEN` + `GRAFANA_STACK_URL` | Receiver (consultas `/status` do #16 no Mimir) | `.env` EC2 — o token de **leitura**, não o de escrita |
| `HERMES_WEBHOOK_URL` | Receiver → Hermes (issue #15) | `.env` EC2 — não é segredo |
| `HERMES_WEBHOOK_TOKEN` | Receiver → Hermes (`X-Hermes-Token` + `Authorization: Bearer`) | SSM `/ops-centro/prod/` → `.env` da EC2; o mesmo valor no Hermes |
| `HERMES_RETRIES` / `HERMES_BACKOFF` / `HERMES_TIMEOUT` | Entrega ao Hermes (defaults 3 / 0,5s / 5s) | `.env` EC2 — não é segredo |
| `HERMES_RATE_LIMIT` / `HERMES_RATE_WINDOW` | Rate limit anti-tempestade (defaults 10 / 60s) | idem |
| `STATUS_QUERY_TIMEOUT` | Consultas sob demanda (default 8s, issue #16) | idem |
| `AUTONOMOUS_ACTIONS` | Kill switch das ações autônomas (issue #17) | `.env` EC2 — não é segredo, mas é a alavanca de emergência |
| `AUTONOMOUS_PAUSE_THRESHOLD` / `_WINDOW_MINUTES` / `_TTL_SECONDS` | Regra de decisão da pausa (defaults 5 / 30min / 900s) | idem |
| `ADMIN_API_AGENTS_PLATFORM_URL` / `ADMIN_API_FILE_MEMORY_URL` | Endpoint admin de pause/resume de tool nos apps | `.env` EC2 — não é segredo |
| `ADMIN_API_TOKEN` | Auth do receiver no `admin_api` dos apps | SSM `/ops-centro/prod/` → `.env` da EC2; o mesmo valor nos apps |

Na EC2 nenhum destes é digitado à mão: `deploy/env-from-ssm.sh` monta o `.env` (modo 600) a
partir do **AWS SSM Parameter Store** (`/ops-centro/prod/*`, os segredos como
`SecureString`). Rotação = `aws ssm put-parameter` + rodar o script + `./deploy.sh`. Ver
[deploy.md §3](deploy.md#3-segredos-ssm-parameter-store-nunca-arquivo-commitado-rnf06).

O CD publica a imagem no GHCR com o `GITHUB_TOKEN` da própria run (`packages: write`) — não
há PAT envolvido. Se o pacote ficar privado, a EC2 precisa de um PAT com `read:packages`
só para o `docker login`.

## Turso (logs de longa retenção — issue #8)

Database `ops-centro-logs` (free tier). Provisionamento e uso em [turso-logs.md](turso-logs.md).

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `TURSO_DATABASE_URL` | `libsql://ops-centro-logs-<org>.turso.io` | Writer de logs (apps) e enriquecimento (receiver) | `.env` local/EC2 + secrets dos 3 repos |
| `TURSO_AUTH_TOKEN` | token do database (`turso db tokens create`) | idem | idem |

Rotação: `turso db tokens create ops-centro-logs` → atualizar secrets/`.env` →
`turso db tokens invalidate ops-centro-logs` para revogar os antigos.

O job de retenção (issue #9) roda no GitHub Actions e usa `TURSO_DATABASE_URL`,
`TURSO_AUTH_TOKEN` e os dois `OTEL_EXPORTER_OTLP_*` como **secrets do repo**; a janela de
retenção (`TURSO_LOG_RETENTION_DAYS`) é uma *repository variable*, não secret — não há
segredo em "ERROR fica 90 dias". Ver [turso-retencao.md](turso-retencao.md).
