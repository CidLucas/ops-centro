"""Convenções de telemetria compartilhadas entre os apps (RF02 / RNF05 do plano).

Fonte de verdade do schema comum: todo sinal (métrica, trace, log) emitido pelos
apps deve carregar os atributos definidos aqui. Os apps consomem este módulo (ou
replicam as constantes) para garantir queries e dashboards cruzados no Grafana.

Apps conhecidos:
- agents-platform  → repo_platform (services/agent_api, tool_pool_api)
- file-memory-mcp  → mcp_brain (mcp_server, ingestion, graph)
- ops-centro       → este repo (receiver de alertas)
"""

from __future__ import annotations

import os

# --- Nomes de atributos (RF02) -----------------------------------------------
ATTR_APP_NAME = "app_name"
ATTR_ENVIRONMENT = "environment"
ATTR_TENANT_ID = "tenant_id"
ATTR_VERSION = "version"

# --- Valores canônicos de app_name -------------------------------------------
APP_AGENTS_PLATFORM = "agents-platform"
APP_FILE_MEMORY = "file-memory-mcp"
APP_OPS_CENTRO = "ops-centro"

KNOWN_APPS = frozenset({APP_AGENTS_PLATFORM, APP_FILE_MEMORY, APP_OPS_CENTRO})

# --- Ambientes canônicos ------------------------------------------------------
ENV_DEV = "dev"
ENV_STAGING = "staging"
ENV_PROD = "prod"

KNOWN_ENVIRONMENTS = frozenset({ENV_DEV, ENV_STAGING, ENV_PROD})

# --- Nomes de spans/eventos (seção 6 do plano) --------------------------------
SPAN_AGENT_EXECUTION = "agent_execution"
SPAN_MCP_TOOL_CALL = "mcp_tool_call"
SPAN_FILE_INGESTION = "file_ingestion"
SPAN_MCP_MEMORY_QUERY = "mcp_memory_query"

# --- Atributos de span (seção 6 do plano; ver docs/schema.md) ------------------
# agent_execution
ATTR_AGENT_NAME = "agent_name"
ATTR_MODEL = "model"
ATTR_TOKENS_INPUT = "tokens_input"
ATTR_TOKENS_OUTPUT = "tokens_output"
ATTR_COST_USD = "cost_usd"
ATTR_SESSION_ID = "session_id"
# mcp_tool_call
ATTR_TOOL_NAME = "tool_name"
ATTR_MCP_SERVER = "mcp_server"
ATTR_RETRIES = "retries"
# file_ingestion
ATTR_FILE_ID = "file_id"
ATTR_FILE_TYPE = "file_type"
ATTR_FILE_SIZE_BYTES = "file_size_bytes"
ATTR_INGESTION_STAGE = "stage"
# mcp_memory_query
ATTR_MCP_SERVER_ID = "mcp_server_id"
ATTR_QUERY_TYPE = "query_type"
ATTR_RESULT_COUNT = "result_count"

# --- Métricas: prefixos por app (docs/schema.md) -------------------------------
# Prefixo snake_case por app; counters terminam em `_total`, histogramas de
# latência em `_duration_seconds` (unit "s"), tamanhos em `_bytes`.
# `context_mcp` já está em produção no mcp_brain — congelado como está.
METRIC_PREFIXES = {
    APP_AGENTS_PLATFORM: "agents_platform",
    APP_FILE_MEMORY: "context_mcp",
    APP_OPS_CENTRO: "ops_centro",
}

# Labels permitidas em métricas (cardinalidade baixa e enumerável). tenant_id é
# permitido apenas em métricas de volume/uso; session_id/trace_id/file_id nunca.
# `query_type` e `level` entraram na v1.1 (issue #11) — os dois são enumeráveis e
# necessários para as métricas da §7 do serviço de memória e do job de retenção.
ALLOWED_METRIC_LABELS = frozenset(
    {
        ATTR_APP_NAME,
        ATTR_ENVIRONMENT,
        ATTR_TENANT_ID,
        "tool",
        "stage",
        "status",
        "agent",
        "model",
        "query_type",
        "level",
    }
)


def build_resource_attributes(
    app_name: str,
    *,
    version: str,
    environment: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, str]:
    """Monta o dicionário de atributos comuns exigidos pelo RF02.

    `environment` cai para a env var ENVIRONMENT (default: dev). `tenant_id` é
    opcional em resource attributes — sinais por-request devem setá-lo por span.
    Levanta ValueError para app/environment fora do vocabulário canônico, para
    impedir divergência silenciosa de schema entre os repos (RNF05).
    """
    if app_name not in KNOWN_APPS:
        raise ValueError(f"app_name desconhecido: {app_name!r} (esperado um de {sorted(KNOWN_APPS)})")

    env = environment or os.environ.get("ENVIRONMENT", ENV_DEV)
    if env not in KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"environment desconhecido: {env!r} (esperado um de {sorted(KNOWN_ENVIRONMENTS)})"
        )

    attrs = {
        ATTR_APP_NAME: app_name,
        ATTR_ENVIRONMENT: env,
        ATTR_VERSION: version,
    }
    if tenant_id:
        attrs[ATTR_TENANT_ID] = tenant_id
    return attrs
