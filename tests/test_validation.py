"""Testes do roteiro de validação do Grafana Cloud (issue #6).

As respostas dos datasources são simuladas: o que se testa aqui é a *lógica do gate* —
o que conta como PASS, o que conta como FALHA e o que é apenas pulado por falta de
credencial. As queries em si são conferidas contra o texto que vai para o Explore.
"""

import httpx
import pytest

from ops_centro import validation as v

pytestmark = pytest.mark.unit


def endpoint_falso(handler) -> v.Endpoint:
    """Endpoint cujo GET responde via `handler(path, params)`."""

    class FakeEndpoint(v.Endpoint):
        def get(self, path, params):
            request = httpx.Request("GET", f"{self.url}{path}", params=params)
            return handler(path, params, request)

    return FakeEndpoint("https://exemplo.grafana.net", "1234", "glc_token")


def json_response(request, payload, status=200):
    return httpx.Response(status, json=payload, request=request)


def cloud_com(prom=None, loki=None, tempo=None) -> v.GrafanaCloud:
    vazio = v.Endpoint("", "", "")
    return v.GrafanaCloud(prom=prom or vazio, loki=loki or vazio, tempo=tempo or vazio)


# --- sem credenciais ----------------------------------------------------------
def test_sem_credenciais_tudo_e_pulado_e_nao_reprova(monkeypatch, capsys):
    for var in (
        "GRAFANA_READ_TOKEN", "GRAFANA_PROM_URL", "GRAFANA_PROM_USER",
        "GRAFANA_LOKI_URL", "GRAFANA_LOKI_USER", "GRAFANA_TEMPO_URL", "GRAFANA_TEMPO_USER",
    ):
        monkeypatch.delenv(var, raising=False)

    assert v.main([]) == 0  # gate não executado ≠ gate reprovado
    saida = capsys.readouterr().out
    assert "SKIP" in saida
    assert "token de LEITURA" in saida


# --- métricas ------------------------------------------------------------------
def test_metricas_passam_com_series_e_rf02_completos():
    def handler(path, params, request):
        if params["query"].startswith("count(count by"):
            return json_response(request, {"data": {"result": [{"value": [0, "12"]}]}})
        return json_response(request, {"data": {"result": [
            {"metric": {"app_name": "agents-platform", "environment": "dev", "version": "0.3.0"}}
        ]}})

    resultados = v.check_metrics(cloud_com(prom=endpoint_falso(handler)), "agents-platform")
    assert [r.ok for r in resultados] == [True, True]
    assert "agents_platform_.*" in resultados[0].query
    assert "12 série(s)" in resultados[0].detail


def test_metricas_reprovam_quando_falta_atributo_do_rf02():
    def handler(path, params, request):
        if params["query"].startswith("count(count by"):
            return json_response(request, {"data": {"result": [{"value": [0, "3"]}]}})
        # série sem `version` — RF02 incompleto
        return json_response(request, {"data": {"result": [
            {"metric": {"app_name": "file-memory-mcp", "environment": "dev"}}
        ]}})

    _series, rf02 = v.check_metrics(cloud_com(prom=endpoint_falso(handler)), "file-memory-mcp")
    assert rf02.ok is False
    assert "version" in rf02.detail


def test_metricas_reprovam_sem_serie_alguma():
    def handler(path, params, request):
        return json_response(request, {"data": {"result": []}})

    series, rf02 = v.check_metrics(cloud_com(prom=endpoint_falso(handler)), "ops-centro")
    assert (series.ok, rf02.ok) == (False, False)
    assert "0 série(s)" in series.detail


# --- logs ----------------------------------------------------------------------
def test_logs_somam_as_linhas_da_janela():
    def handler(path, params, request):
        return json_response(request, {"data": {"result": [
            {"values": [[0, "7"], [1, "5"]]},
        ]}})

    (resultado,) = v.check_logs(cloud_com(loki=endpoint_falso(handler)), "agents-platform")
    assert resultado.ok is True
    assert "12 linha(s)" in resultado.detail


def test_erro_http_vira_falha_com_a_mensagem_do_backend():
    def handler(path, params, request):
        return httpx.Response(401, text="unauthorized", request=request)

    (resultado,) = v.check_logs(cloud_com(loki=endpoint_falso(handler)), "agents-platform")
    assert resultado.ok is False
    assert "HTTP 401" in resultado.detail


def test_erro_de_rede_nao_interrompe_o_roteiro():
    def handler(path, params, request):
        raise httpx.ConnectError("dns falhou", request=request)

    (resultado,) = v.check_logs(cloud_com(loki=endpoint_falso(handler)), "ops-centro")
    assert resultado.ok is False
    assert "erro de rede" in resultado.detail


# --- traces ---------------------------------------------------------------------
def test_traces_passam_quando_todos_carregam_o_rf02():
    def handler(path, params, request):
        # As duas buscas (com e sem os atributos) devolvem os mesmos 2 traces.
        return json_response(request, {"traces": [{"traceID": "a"}, {"traceID": "b"}]})

    presenca, rf02 = v.check_traces(cloud_com(tempo=endpoint_falso(handler)), "agents-platform")
    assert (presenca.ok, rf02.ok) == (True, True)
    assert "2/2 traces" in rf02.detail
    assert 'resource.app_name="agents-platform"' in presenca.query


def test_traces_reprovam_quando_parte_nao_tem_os_atributos():
    def handler(path, params, request):
        completo = "environment" in params["q"]
        traces = [{"traceID": "a"}] if completo else [{"traceID": "a"}, {"traceID": "b"}]
        return json_response(request, {"traces": traces})

    _presenca, rf02 = v.check_traces(cloud_com(tempo=endpoint_falso(handler)), "agents-platform")
    assert rf02.ok is False
    assert "1/2" in rf02.detail


def test_cruzamento_por_tenant_lista_os_servicos():
    def handler(path, params, request):
        return json_response(request, {"traces": [
            {"rootServiceName": "agent_api"}, {"rootServiceName": "context-mcp-server"},
        ]})

    (resultado,) = v.check_cross_app(
        cloud_com(tempo=endpoint_falso(handler)), ["agents-platform", "file-memory-mcp"]
    )
    assert resultado.ok is True
    assert "agent_api" in resultado.detail and "context-mcp-server" in resultado.detail
    assert "tenant_id" in resultado.query


# --- agregação -------------------------------------------------------------------
def test_summary_separa_pass_falha_e_skip():
    resultados = [
        v.CheckResult("métricas", "a", "q", True, ""),
        v.CheckResult("logs", "b", "q", False, ""),
        v.CheckResult("traces", "c", "q", False, "", skipped=True),
    ]
    resumo = v.Summary.of(resultados)
    assert (resumo.passed, resumo.failed, resumo.skipped) == (1, 1, 1)
