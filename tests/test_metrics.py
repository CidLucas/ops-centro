"""Testes do catálogo de métricas da §7 (issue #11).

O catálogo é contrato entre três lados (apps, dashboards, checker). Estes testes travam
as regras que impedem os três de divergirem — em especial as de cardinalidade, que são o
risco §10 do plano no free tier.
"""

import httpx
import pytest

from ops_centro import metrics as m
from ops_centro.conventions import (
    ALLOWED_METRIC_LABELS,
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    APP_OPS_CENTRO,
    ATTR_TENANT_ID,
    METRIC_PREFIXES,
)

pytestmark = pytest.mark.unit


# --- integridade do catálogo ---------------------------------------------------
def test_catalogo_respeita_as_regras_do_schema():
    """Prefixo por app, sufixos/unidades, labels dentro do vocabulário e cobertura da §7."""
    assert m.validate_catalog() == []


def test_todo_item_da_secao_7_tem_metrica():
    for item in m.PLANO_ITEMS:
        assert m.by_item(item), f"item da §7 sem métrica: {item}"


def test_labels_de_alta_cardinalidade_sao_proibidas():
    proibidas = {"session_id", "trace_id", "file_id", "message"}
    for metric in m.CATALOG:
        assert not (proibidas & set(metric.labels)), f"{metric.name} usa label proibida"
        assert set(metric.labels) <= ALLOWED_METRIC_LABELS


def test_tenant_id_so_em_metrica_de_volume():
    """`tenant_id` fora de métrica de volume multiplica séries — é o jeito clássico de
    estourar as 10k séries ativas do free tier."""
    com_tenant = [metric for metric in m.CATALOG if ATTR_TENANT_ID in metric.labels]
    assert com_tenant, "a §7 pede volume por tenant — alguma métrica precisa carregá-lo"
    for metric in com_tenant:
        assert metric.item == m.ITEM_TENANT_VOLUME


def test_validate_catalog_pega_violacao_injetada():
    """A validação precisa reprovar de verdade, não só passar no catálogo bom."""
    ruim = (
        m.Metric(
            name="agents_platform_coisas",  # counter sem _total
            app=APP_AGENTS_PLATFORM,
            kind=m.COUNTER,
            unit="1",
            labels=("session_id",),  # label fora do schema
            item=m.ITEM_ERROR_RATE,
            description="",
        ),
    )
    problemas = m.validate_catalog(ruim)
    assert any("_total" in p for p in problemas)
    assert any("fora do schema" in p for p in problemas)
    # catálogo parcial: os itens da §7 não cobertos também são reportados
    assert any("sem métrica no catálogo" in p for p in problemas)


def test_prefixo_de_metrica_bate_com_o_app():
    for metric in m.CATALOG:
        assert metric.name.startswith(METRIC_PREFIXES[metric.app] + "_")


def test_serie_de_histograma_vira_bucket():
    latencia = m.BY_NAME["context_mcp_tool_call_duration_seconds"]
    assert latencia.sample_series() == "context_mcp_tool_call_duration_seconds_bucket"
    counter = m.BY_NAME["context_mcp_tool_calls_total"]
    assert counter.sample_series() == counter.name


def test_metricas_ja_em_producao_no_mcp_brain_estao_congeladas():
    """Estas quatro já existem no `common/observability.py` do mcp_brain — mudar o nome
    aqui quebraria séries em produção (schema.md §3: `context_mcp` fica como está)."""
    emitindo = {metric.name for metric in m.by_app(APP_FILE_MEMORY) if metric.status == m.EMITTING}
    assert emitindo == {
        "context_mcp_tool_calls_total",
        "context_mcp_tool_call_duration_seconds",
        "context_mcp_ingestion_stage_total",
        "context_mcp_ingestion_stage_duration_seconds",
    }


def test_heartbeat_esta_no_catalogo_como_metrica_de_operacao():
    """`ops_centro_heartbeat_total` (issue #27) é série de operação do próprio pipeline,
    como as outras `ops_centro_*`: já emitida pelo receiver (status=EMITTING), e sem
    labels próprias — o batimento não tem dimensão a fatiar."""
    metrica = m.BY_NAME["ops_centro_heartbeat_total"]
    assert metrica.app == APP_OPS_CENTRO
    assert metrica.kind == m.COUNTER
    assert metrica.labels == ()
    assert metrica.item == m.ITEM_SELF
    assert metrica.status == m.EMITTING


# --- checker contra o Prometheus ------------------------------------------------
class FakeEndpoint:
    """Datasource falso: devolve um corpo de `/api/v1/query` por nome de métrica."""

    def __init__(self, por_metrica: dict[str, list[dict]]):
        self.por_metrica = por_metrica
        self.queries: list[str] = []

    configured = True

    def get(self, path, params):
        self.queries.append(params["query"])
        nome = next((n for n in self.por_metrica if n in params["query"]), None)
        series = self.por_metrica.get(nome, [])
        return httpx.Response(
            200,
            json={"data": {"result": [{"metric": s, "value": [0, "1"]} for s in series]}},
            request=httpx.Request("GET", "http://fake"),
        )


class FakeCloud:
    def __init__(self, prom):
        self.prom = prom


def _resultado(resultados, nome):
    return next(r for r in resultados if r.name == nome)


def test_check_passa_quando_a_serie_tem_todos_os_labels():
    prom = FakeEndpoint(
        {
            "context_mcp_tool_calls_total": [
                {"app_name": "file-memory-mcp", "environment": "prod", "tool": "search",
                 "status": "ok"}
            ]
        }
    )
    resultados = m.check_catalog(FakeCloud(prom), [APP_FILE_MEMORY])
    resultado = _resultado(resultados, "context_mcp_tool_calls_total")
    assert resultado.ok and not resultado.skipped
    # a query impressa é colável no Explore — é o que torna o gate auditável
    assert "count by (app_name, environment, tool, status)" in resultado.query


def test_check_falha_quando_falta_label_comum():
    prom = FakeEndpoint(
        {"context_mcp_tool_calls_total": [{"app_name": "file-memory-mcp", "tool": "search",
                                           "status": "ok"}]}
    )
    resultado = _resultado(
        m.check_catalog(FakeCloud(prom), [APP_FILE_MEMORY]), "context_mcp_tool_calls_total"
    )
    assert not resultado.ok
    assert "environment" in resultado.detail


def test_check_falha_quando_nao_ha_serie_nenhuma():
    resultado = _resultado(
        m.check_catalog(FakeCloud(FakeEndpoint({})), [APP_FILE_MEMORY]),
        "context_mcp_tool_calls_total",
    )
    assert not resultado.ok and not resultado.skipped
    assert "não está emitindo" in resultado.detail


def test_metrica_pendente_vira_skip_e_nao_falha():
    """Backlog conhecido (#5/#7) não pode virar falha vermelha sem informação."""
    resultados = m.check_catalog(FakeCloud(FakeEndpoint({})), [APP_AGENTS_PLATFORM])
    assert resultados and all(r.skipped for r in resultados)
    assert all("pendente" in r.detail for r in resultados)


# --- RF02 em métricas ------------------------------------------------------------
def test_common_labels_traz_app_e_ambiente(monkeypatch):
    """O Grafana Cloud não promove resource attribute a label de métrica (medido no
    stack real): `app_name`/`environment` têm que viajar no ponto."""
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert m.common_labels(APP_FILE_MEMORY) == {
        "app_name": "file-memory-mcp",
        "environment": "prod",
    }


def test_common_labels_nao_carrega_version():
    """`version` mudaria a cada deploy e criaria série nova a cada release — por isso
    não está em ALLOWED_METRIC_LABELS nem aqui."""
    assert "version" not in m.common_labels(APP_FILE_MEMORY, "dev")
    assert set(m.common_labels(APP_FILE_MEMORY, "dev")) <= ALLOWED_METRIC_LABELS
