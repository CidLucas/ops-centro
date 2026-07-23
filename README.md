# Ops Centro

Serviço de observabilidade centralizada dos dois produtos ativos, com custo alvo ~R$ 0:

- **Agents Platform** ([repo_platform](https://github.com/CidLucas/repo_platform)) — agentes + MCP
- **File Memory / MCP** ([mcp_brain](https://github.com/CidLucas/mcp_brain)) — ingestão de arquivos + memória via MCP

Plano completo em [plano.md](plano.md). Backlog de implantação nas [issues](../../issues), organizadas em milestones por fase.

## Arquitetura (resumo)

```
apps ──OTLP──▶ Grafana Cloud (Prom + Loki + Tempo, free tier)
                   │
        dashboards │ alertas (webhook) ─▶ ops-centro receiver ─▶ Hermes (EC2) ─▶ Telegram
                   │                            │
                   └── logs longos ─▶ Turso ◀───┘ (enriquecimento por trace_id)
```

## O que vive neste repo

| Área | Onde | O quê |
| --- | --- | --- |
| Convenções de telemetria | [`ops_centro/conventions.py`](ops_centro/conventions.py) | Schema comum (RF02/RNF05): `app_name`, `environment`, `tenant_id`, `version` + nomes canônicos de spans |
| Catálogo de métricas | [`ops_centro/metrics.py`](ops_centro/metrics.py) | As métricas prioritárias da §7 com tipo, unidade e labels fechadas — gera os painéis e é conferível contra o Prometheus ([docs](docs/metricas-prioritarias.md)) |
| Receiver de alertas | [`ops_centro/receiver/`](ops_centro/receiver/) | FastAPI que recebe o webhook do Grafana, enriquece via Turso e aciona o Hermes |
| Migrations Turso | [`db/migrations/`](db/migrations/) | Tabela `logs` de longa retenção correlacionada por `trace_id` (RF05) |
| Writer de logs | [`ops_centro/turso/`](ops_centro/turso/) | `log_to_turso(...)` em batch numa thread daemon (RNF04) + aplicador de migrations — ver [docs/turso-logs.md](docs/turso-logs.md) |
| Retenção dos logs | [`ops_centro/turso/retention.py`](ops_centro/turso/retention.py) | Janela por nível + job diário de limpeza, com métricas próprias e alerta de teto do free tier ([docs](docs/turso-retencao.md)) |
| Dashboards as-code | [`ops_centro/grafana/`](ops_centro/grafana/) → [`grafana/dashboards/`](grafana/dashboards/) | Quatro dashboards gerados a partir do catálogo e publicados por API de forma idempotente ([docs](docs/dashboards.md)) |
| Alertas as-code | [`grafana/alerts/`](grafana/alerts/) | Regras em formato de provisionamento do Grafana Alerting |
| Validação da Fase 1 | [`ops_centro/validation.py`](ops_centro/validation.py) | Checklist executável de chegada de sinais no Grafana Cloud — [roteiro](docs/validacao-fase1.md) e [baseline de free tier](docs/free-tier-baseline.md) |

A instrumentação dos apps usa a lib [`blu_observability_bootstrap`](https://github.com/CidLucas/repo_platform/tree/main/libs/blu_observability_bootstrap) do repo_platform, consumida aqui como dependência git pinada por commit (ver `[tool.uv.sources]` no [pyproject.toml](pyproject.toml)).

## Desenvolvimento

```bash
make env       # cria .env a partir do .env.example
make install   # uv sync --frozen --extra dev
make lint      # ruff (mesmo gate do CI)
make test      # pytest -m unit (mesmo gate do CI)
make run       # receiver local em :8080
make migrate   # aplica as migrations no Turso (docs/turso-logs.md)
make validate  # checklist de sinais no Grafana Cloud (docs/validacao-fase1.md)
make up        # stack via docker compose
```

Fase 2 (`make help` lista tudo):

```bash
make metrics          # catálogo de métricas da §7 (docs/metricas-prioritarias.md)
make metrics-check    # confere no Prometheus se essas métricas estão chegando
make dashboards       # (re)gera grafana/dashboards/*.json (docs/dashboards.md)
make dashboards-apply # publica no Grafana Cloud (idempotente)
make retention-dry    # o que a retenção de logs apagaria (docs/turso-retencao.md)
```

## CI/CD

Mesmo modelo do mcp_brain:

- **[ci.yml](.github/workflows/ci.yml)** — lint (ruff), varredura de segredos (gitleaks), audit de deps (pip-audit, não-bloqueante) e testes unit com piso de cobertura. Roda em todo push e PR para `main`.
- **[cd.yml](.github/workflows/cd.yml)** — build local da imagem Docker, smoke (entrypoints importam, roda non-root) e validação do compose. Roda em push na `main` e tags `v*`.
- **[retention.yml](.github/workflows/retention.yml)** — job diário de limpeza dos logs no Turso (issue #9); sobe o relatório de cada run como artefato.
- **Proteção da `main`** — ruleset versionado em [.github/rulesets/protect-main.json](.github/rulesets/protect-main.json); status em [.github/BRANCH-PROTECTION.md](.github/BRANCH-PROTECTION.md).
