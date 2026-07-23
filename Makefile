# Makefile — operações de desenvolvimento do Ops Centro.
# Uso: `make <alvo>`. Rode `make help` para a lista completa.

COMPOSE := docker compose
RECEIVER_PORT ?= 8080

.DEFAULT_GOAL := help

# ---------------------------------------------------------------- ambiente ---
.PHONY: install env
install: ## Sincroniza o venv com o lockfile (projeto + deps de dev)
	uv sync --frozen --extra dev

env: ## Cria .env a partir do .env.example (não sobrescreve)
	@test -f .env && echo ".env já existe — nada a fazer" || (cp .env.example .env && echo ".env criado — preencha as chaves")

# --------------------------------------------------------------- qualidade ---
.PHONY: lint test cov
lint: ## ruff check (mesmo gate do CI)
	uv run ruff check .

test: ## Testes unitários (determinísticos, mesmo gate do CI)
	uv run pytest -m unit

cov: ## Testes com relatório de cobertura
	uv run pytest -m unit --cov --cov-report=term-missing

# ----------------------------------------------------------- entrypoints ----
.PHONY: run
run: ## Sobe o receiver local (porta $(RECEIVER_PORT))
	uv run uvicorn ops_centro.receiver.app:app --reload --port $(RECEIVER_PORT)

# ---------------------------------------------------------------- turso -----
.PHONY: migrate migrate-status retention retention-dry retention-vacuum
migrate: ## Aplica as migrations no Turso (TURSO_DATABASE_URL)
	uv run python -m ops_centro.turso.migrate

migrate-status: ## Lista as migrations já aplicadas
	uv run python -m ops_centro.turso.migrate --status

retention: ## Aplica a política de retenção dos logs (issue #9)
	uv run python -m ops_centro.turso.retention

retention-dry: ## Conta o que a retenção apagaria, sem apagar
	uv run python -m ops_centro.turso.retention --dry-run

retention-vacuum: ## Retenção + VACUUM (caro; devolve o espaço ao disco)
	uv run python -m ops_centro.turso.retention --vacuum

# --------------------------------------------------------- grafana as-code --
.PHONY: dashboards dashboards-check dashboards-apply metrics metrics-check
dashboards: ## (Re)gera os JSONs de grafana/dashboards/ (issue #10)
	uv run python -m ops_centro.grafana.dashboards --write

dashboards-check: ## Falha se os JSONs divergirem do gerador (mesmo gate do teste)
	uv run python -m ops_centro.grafana.dashboards --check

dashboards-apply: ## Publica os dashboards no Grafana Cloud (GRAFANA_API_TOKEN)
	uv run python -m ops_centro.grafana.dashboards --apply

metrics: ## Lista o catálogo de métricas da §7 (issue #11)
	uv run python -m ops_centro.metrics

metrics-check: ## Confere no Prometheus se as métricas da §7 estão chegando
	uv run python -m ops_centro.metrics --check

# ---------------------------------------------------------- validação -------
.PHONY: validate validate-json
validate: ## Checklist de sinais no Grafana Cloud (issue #6)
	uv run python -m ops_centro.validation

validate-json: ## Mesmo checklist em JSON (para o baseline de free tier)
	uv run python -m ops_centro.validation --json

# -------------------------------------------------------------- docker ------
.PHONY: up down logs ps build
up: ## Sobe a stack em background
	$(COMPOSE) up -d --build

down: ## Derruba a stack
	$(COMPOSE) down

logs: ## Segue os logs
	$(COMPOSE) logs -f

ps: ## Status dos serviços
	$(COMPOSE) ps

build: ## (Re)builda as imagens
	$(COMPOSE) build

# ------------------------------------------------------------- utilidades ---
.PHONY: clean
clean: ## Remove caches de build/pytest e __pycache__
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage coverage.xml htmlcov build dist *.egg-info

# ------------------------------------------------------------------ help ----
.PHONY: help
help: ## Lista os alvos disponíveis
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
