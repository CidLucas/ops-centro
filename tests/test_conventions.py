"""Testes do schema comum de telemetria (RF02/RNF05)."""

import pytest

from ops_centro.conventions import (
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    APP_OPS_CENTRO,
    ATTR_APP_NAME,
    ATTR_ENVIRONMENT,
    ATTR_TENANT_ID,
    ATTR_VERSION,
    ENV_PROD,
    build_resource_attributes,
)

pytestmark = pytest.mark.unit


def test_atributos_obrigatorios_do_rf02():
    attrs = build_resource_attributes(APP_AGENTS_PLATFORM, version="1.2.3", environment=ENV_PROD)
    assert attrs[ATTR_APP_NAME] == APP_AGENTS_PLATFORM
    assert attrs[ATTR_ENVIRONMENT] == ENV_PROD
    assert attrs[ATTR_VERSION] == "1.2.3"
    # tenant_id é opcional em resource attributes
    assert ATTR_TENANT_ID not in attrs


def test_tenant_id_quando_informado():
    attrs = build_resource_attributes(
        APP_FILE_MEMORY, version="0.1.0", environment="dev", tenant_id="acme"
    )
    assert attrs[ATTR_TENANT_ID] == "acme"


def test_environment_cai_para_env_var(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    attrs = build_resource_attributes(APP_OPS_CENTRO, version="0.1.0")
    assert attrs[ATTR_ENVIRONMENT] == "staging"


def test_environment_default_dev(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    attrs = build_resource_attributes(APP_OPS_CENTRO, version="0.1.0")
    assert attrs[ATTR_ENVIRONMENT] == "dev"


def test_app_desconhecido_falha():
    with pytest.raises(ValueError, match="app_name desconhecido"):
        build_resource_attributes("app-inventado", version="0.1.0")


def test_environment_desconhecido_falha():
    with pytest.raises(ValueError, match="environment desconhecido"):
        build_resource_attributes(APP_OPS_CENTRO, version="0.1.0", environment="producao")


def test_paridade_schema_congelado_v1():
    """Pina os valores literais do schema v1 (docs/schema.md §4).

    Os consumidores replicam estas constantes e mantêm um teste idêntico —
    os dois lados só podem mudar juntos, via PR aqui primeiro.
    """
    from ops_centro import conventions as c

    assert c.ATTR_APP_NAME == "app_name"
    assert c.ATTR_ENVIRONMENT == "environment"
    assert c.ATTR_TENANT_ID == "tenant_id"
    assert c.ATTR_VERSION == "version"

    assert c.APP_AGENTS_PLATFORM == "agents-platform"
    assert c.APP_FILE_MEMORY == "file-memory-mcp"
    assert c.APP_OPS_CENTRO == "ops-centro"

    assert c.SPAN_AGENT_EXECUTION == "agent_execution"
    assert c.SPAN_MCP_TOOL_CALL == "mcp_tool_call"
    assert c.SPAN_FILE_INGESTION == "file_ingestion"
    assert c.SPAN_MCP_MEMORY_QUERY == "mcp_memory_query"

    assert c.ATTR_AGENT_NAME == "agent_name"
    assert c.ATTR_MODEL == "model"
    assert c.ATTR_TOKENS_INPUT == "tokens_input"
    assert c.ATTR_TOKENS_OUTPUT == "tokens_output"
    assert c.ATTR_COST_USD == "cost_usd"
    assert c.ATTR_SESSION_ID == "session_id"
    assert c.ATTR_TOOL_NAME == "tool_name"
    assert c.ATTR_MCP_SERVER == "mcp_server"
    assert c.ATTR_RETRIES == "retries"

    assert c.METRIC_PREFIXES == {
        "agents-platform": "agents_platform",
        "file-memory-mcp": "context_mcp",
        "ops-centro": "ops_centro",
    }
