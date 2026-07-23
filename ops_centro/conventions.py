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
