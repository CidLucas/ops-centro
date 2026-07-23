# Mapa de secrets (RNF06)

Nenhum valor vive em código — só nomes e onde cada um é usado. Valores ficam em
`.env` local (gitignorado), GitHub Actions secrets e no `.env` da EC2 do Hermes.

## Grafana Cloud

Stack: `stack-1733152-otel-dev` · org `1853471` · região `prod-sa-east-1` (São Paulo).
Gateway OTLP: `https://otlp-gateway-prod-sa-east-1.grafana.net/otlp` (auth Basic `1733152:<token glc_...>`).

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL do gateway OTLP | `blu_observability_bootstrap` nos 3 apps | Secrets de `ops-centro`, `mcp_brain`, `repo_platform` + `.env` local/EC2 |
| `OTEL_EXPORTER_OTLP_HEADERS` | `Authorization=Basic%20<base64(1733152:token)>` (URL-encoded) | idem | idem |

Rotação: gerar novo token em Grafana Cloud → Access Policies, atualizar os
secrets nos 3 repos (`gh secret set`) e os `.env` locais/EC2. O token atual é de
**dev**; criar token separado para prod quando houver ambiente prod.

## Receiver / Hermes

| Variável | Quem usa | Onde está |
| --- | --- | --- |
| `ALERT_WEBHOOK_TOKEN` | Receiver (valida `X-Alert-Token`) e contact point do Grafana | `.env` local/EC2 + config do contact point (issue #12) |
| `HERMES_WEBHOOK_URL` | Receiver → Hermes | `.env` EC2 (fase 3) |

## Turso (logs de longa retenção — issue #8)

Database `ops-centro-logs` (free tier). Provisionamento e uso em [turso-logs.md](turso-logs.md).

| Variável | Formato | Quem usa | Onde está |
| --- | --- | --- | --- |
| `TURSO_DATABASE_URL` | `libsql://ops-centro-logs-<org>.turso.io` | Writer de logs (apps) e enriquecimento (receiver) | `.env` local/EC2 + secrets dos 3 repos |
| `TURSO_AUTH_TOKEN` | token do database (`turso db tokens create`) | idem | idem |

Rotação: `turso db tokens create ops-centro-logs` → atualizar secrets/`.env` →
`turso db tokens invalidate ops-centro-logs` para revogar os antigos.
