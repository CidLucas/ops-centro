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
| Receiver de alertas | [`ops_centro/receiver/`](ops_centro/receiver/) | FastAPI que recebe o webhook do Grafana, enriquece via Turso e aciona o Hermes |
| Migrations Turso | [`db/migrations/`](db/migrations/) | Tabela `logs` de longa retenção correlacionada por `trace_id` (RF05) |
| Writer de logs | [`ops_centro/turso/`](ops_centro/turso/) | `log_to_turso(...)` em batch numa thread daemon (RNF04) + aplicador de migrations — ver [docs/turso-logs.md](docs/turso-logs.md) |
| Dashboards/alertas as-code | `grafana/` | JSON de dashboards e regras de alerta versionados (fase 2/3) |

A instrumentação dos apps usa a lib [`blu_observability_bootstrap`](https://github.com/CidLucas/repo_platform/tree/main/libs/blu_observability_bootstrap) do repo_platform, consumida aqui como dependência git pinada por commit (ver `[tool.uv.sources]` no [pyproject.toml](pyproject.toml)).

## Desenvolvimento

```bash
make env       # cria .env a partir do .env.example
make install   # uv sync --frozen --extra dev
make lint      # ruff (mesmo gate do CI)
make test      # pytest -m unit (mesmo gate do CI)
make run       # receiver local em :8080
make migrate   # aplica as migrations no Turso (docs/turso-logs.md)
make up        # stack via docker compose
```

## CI/CD

Mesmo modelo do mcp_brain:

- **[ci.yml](.github/workflows/ci.yml)** — lint (ruff), varredura de segredos (gitleaks), audit de deps (pip-audit, não-bloqueante) e testes unit com piso de cobertura. Roda em todo push e PR para `main`.
- **[cd.yml](.github/workflows/cd.yml)** — build local da imagem Docker, smoke (entrypoints importam, roda non-root), scan Trivy (não-bloqueante) e validação do compose. Roda em push na `main` e tags `v*`.
- **Proteção da `main`** — ruleset versionado em [.github/rulesets/protect-main.json](.github/rulesets/protect-main.json); status em [.github/BRANCH-PROTECTION.md](.github/BRANCH-PROTECTION.md).
