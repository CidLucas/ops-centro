"""Testes dos dashboards as-code (issue #10).

Dois grupos de garantia:

1. **Deriva** — o JSON commitado em `grafana/dashboards/` é o que o gerador produz hoje.
   Sem isso as duas fontes divergem em silêncio e o "reproduzível do zero" morre.
2. **Correção das queries** — painel com nome de métrica errado não dá erro no Grafana:
   dá gráfico vazio, indistinguível de "não houve tráfego". Aqui isso vira teste.
"""

import json
import re

import httpx
import pytest

from ops_centro.conventions import ALLOWED_METRIC_LABELS
from ops_centro.grafana import dashboards as d
from ops_centro.metrics import BY_NAME

pytestmark = pytest.mark.unit

TODOS = d.build_all()


def exprs(dash) -> list[str]:
    return [
        alvo["expr"]
        for painel in dash["panels"]
        for alvo in painel.get("targets", [])
        if "expr" in alvo
    ]


# --- deriva ---------------------------------------------------------------------
def test_json_commitado_esta_em_dia_com_o_gerador():
    """Se este teste falhar: `make dashboards` e commite o JSON junto da mudança."""
    assert d.check_drift() == []


def test_todos_os_quatro_dashboards_existem_no_repo():
    assert set(TODOS) == {"visao-geral", "por-tenant", "agents-platform", "file-memory"}
    for nome in TODOS:
        assert (d.DASHBOARDS_DIR / f"{nome}.json").exists()


def test_json_gerado_e_estavel():
    """Duas gerações seguidas produzem bytes idênticos — diff de PR só muda quando o
    dashboard muda de verdade."""
    assert d.render(d.build_visao_geral()) == d.render(d.build_visao_geral())


# --- invariantes de estrutura -----------------------------------------------------
@pytest.mark.parametrize("nome", sorted(TODOS))
def test_dashboard_tem_uid_titulo_e_variavel_de_ambiente(nome):
    dash = TODOS[nome]
    assert dash["uid"].startswith("ops-centro-")
    assert dash["title"] and dash["description"]
    variaveis = {v["name"] for v in dash["templating"]["list"]}
    assert "DS_PROM" in variaveis  # datasource parametrizado (portável entre stacks)
    assert "environment" in variaveis


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_dashboard_nao_e_editavel_na_ui(nome):
    """A fonte de verdade é o gerador — edição na UI seria apagada sem aviso."""
    assert TODOS[nome]["editable"] is False


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_uids_e_ids_de_painel_sao_unicos(nome):
    ids = [painel["id"] for painel in TODOS[nome]["panels"]]
    assert len(ids) == len(set(ids))


def test_uids_nao_colidem_entre_dashboards():
    uids = [dash["uid"] for dash in TODOS.values()]
    assert len(uids) == len(set(uids))


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_paineis_cabem_na_grade_de_24_colunas(nome):
    for painel in TODOS[nome]["panels"]:
        pos = painel["gridPos"]
        assert pos["x"] + pos["w"] <= 24, f"{painel['title']} estoura a grade"
        assert pos["h"] > 0


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_todo_painel_de_dados_tem_query_e_datasource(nome):
    for painel in TODOS[nome]["panels"]:
        if painel["type"] == "row":
            continue
        assert painel["targets"], f"{painel['title']} sem target"
        assert painel["datasource"] == d.DS
        for alvo in painel["targets"]:
            assert alvo["expr"].strip()


# --- correção das queries ----------------------------------------------------------
@pytest.mark.parametrize("nome", sorted(TODOS))
def test_toda_metrica_citada_esta_no_catalogo(nome):
    fora = sorted(m for m in d.referenced_metrics(TODOS[nome]) if m not in BY_NAME)
    assert fora == [], f"{nome} cita métrica fora do catálogo da §7: {fora}"


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_queries_so_usam_labels_do_schema(nome):
    """Label fora do vocabulário no painel denuncia métrica de alta cardinalidade na
    origem — o risco número um do free tier (§10)."""
    permitidas = ALLOWED_METRIC_LABELS | {"le"}
    for expr in exprs(TODOS[nome]):
        usadas = set(re.findall(r"(\w+)\s*=~?\s*\"", expr))
        for grupo in re.findall(r"by \(([^)]*)\)", expr):
            usadas |= {label.strip() for label in grupo.split(",") if label.strip()}
        assert usadas <= permitidas, f"labels fora do schema em {nome}: {usadas - permitidas}"


@pytest.mark.parametrize("nome", sorted(TODOS))
def test_todo_dashboard_filtra_por_environment(nome):
    for expr in exprs(TODOS[nome]):
        assert 'environment=~"$environment"' in expr


def test_quantil_agrupa_por_le():
    """`histogram_quantile` sem `le` no agrupamento devolve NaN — silenciosamente."""
    for dash in TODOS.values():
        for expr in exprs(dash):
            if "histogram_quantile" not in expr:
                continue
            assert re.search(r"sum by \(le[,)]", expr), expr
            assert "_bucket" in expr


def test_taxa_de_erro_protege_o_denominador():
    """Sem `clamp_min` a divisão por zero some com o painel em vez de mostrar 0%."""
    for dash in TODOS.values():
        for expr in exprs(dash):
            if expr.startswith("100 * "):
                assert "clamp_min(" in expr
                assert 'status="error"' in expr


def test_dashboard_por_tenant_filtra_pelas_duas_variaveis():
    dash = TODOS["por-tenant"]
    assert "tenant_id" in {v["name"] for v in dash["templating"]["list"]}
    for expr in exprs(dash):
        assert 'tenant_id=~"$tenant_id"' in expr


def test_dashboard_por_tenant_cruza_os_dois_apps():
    """Critério de aceite do #10: o mesmo `tenant_id` vale nos dois apps (RNF05)."""
    citadas = d.referenced_metrics(TODOS["por-tenant"])
    assert {BY_NAME[m].app for m in citadas} == {"agents-platform", "file-memory-mcp"}


def test_visao_geral_mostra_os_dois_apps_lado_a_lado():
    citadas = d.referenced_metrics(TODOS["visao-geral"])
    assert {BY_NAME[m].app for m in citadas} == {
        "agents-platform",
        "file-memory-mcp",
        "ops-centro",
    }


def test_agents_platform_tem_o_drill_down_de_tokens_custo_e_modelo():
    citadas = d.referenced_metrics(TODOS["agents-platform"])
    assert {
        "agents_platform_llm_tokens_input_total",
        "agents_platform_llm_tokens_output_total",
        "agents_platform_llm_cost_usd_total",
    } <= citadas


def test_file_memory_tem_funil_de_ingestao_e_latencia_de_query():
    citadas = d.referenced_metrics(TODOS["file-memory"])
    assert "context_mcp_ingestion_stage_total" in citadas
    assert "context_mcp_memory_query_duration_seconds" in citadas


# --- publicação ---------------------------------------------------------------------
class FakeAPI(d.GrafanaAPI):
    """Cliente que registra os POSTs em vez de fazê-los."""

    def __init__(self):
        super().__init__("https://exemplo.grafana.net", "glsa_fake")
        self.chamadas: list[tuple[str, dict]] = []

    def _post(self, path, payload):
        self.chamadas.append((path, payload))
        corpo = {"status": "success", "version": 1, "url": "/d/x"}
        if path == "/api/folders":
            corpo = {"uid": d.FOLDER_UID}
        return httpx.Response(200, json=corpo, request=httpx.Request("POST", path))


def test_aplicacao_publica_os_quatro_com_overwrite():
    api = FakeAPI()
    assert d.apply_all(api) == 0
    caminhos = [caminho for caminho, _ in api.chamadas]
    assert caminhos[0] == "/api/folders"
    assert caminhos.count("/api/dashboards/db") == len(TODOS)
    for caminho, payload in api.chamadas[1:]:
        assert payload["overwrite"] is True  # idempotente: uid fixo + overwrite
        assert payload["folderUid"] == d.FOLDER_UID
        assert payload["dashboard"]["id"] is None  # id de outra instância daria 404


def test_aplicacao_e_idempotente_no_payload():
    """Duas execuções mandam exatamente o mesmo corpo — nada de versão embutida."""
    primeira, segunda = FakeAPI(), FakeAPI()
    d.apply_all(primeira)
    d.apply_all(segunda)
    assert [c for _, c in primeira.chamadas] == [c for _, c in segunda.chamadas]


def test_pasta_ja_existente_nao_e_falha():
    class PastaExistente(FakeAPI):
        def _post(self, path, payload):
            if path == "/api/folders":
                return httpx.Response(
                    409, json={"message": "a folder with the same name already exists"},
                    request=httpx.Request("POST", path),
                )
            return super()._post(path, payload)

    api = PastaExistente()
    assert d.apply_all(api) == 0


class SemPermissao(FakeAPI):
    """Token de leitura (Viewer): lê tudo, não escreve nada — o caso real do
    GRAFANA_READ_TOKEN da Fase 1."""

    def __init__(self, pasta_existe: bool):
        super().__init__()
        self.pasta_existe = pasta_existe

    def _post(self, path, payload):
        self.chamadas.append((path, payload))
        return httpx.Response(
            403,
            json={"message": "Permissions needed: folders:create"},
            request=httpx.Request("POST", path),
        )

    def _get(self, path):
        codigo = 200 if self.pasta_existe else 404
        return httpx.Response(codigo, json={}, request=httpx.Request("GET", path))


def test_403_na_pasta_nao_bloqueia_se_a_pasta_ja_existe():
    """Publicar dentro de uma pasta existente exige `dashboards:write`, não
    `folders:create` — não dá para parar antes de tentar."""
    api = SemPermissao(pasta_existe=True)
    ok, detalhe = api.ensure_folder()
    assert ok and "já existe" in detalhe


def test_403_sem_pasta_explica_qual_permissao_falta(capsys):
    assert d.apply_all(SemPermissao(pasta_existe=False)) == 1
    saida = capsys.readouterr().out
    assert "folders:create" in saida
    assert "GRAFANA_API_TOKEN" in saida  # a saída diz onde arrumar, não só o que faltou


def test_sem_credencial_o_cli_sai_com_erro(monkeypatch, capsys):
    monkeypatch.delenv("GRAFANA_API_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_READ_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_STACK_URL", raising=False)
    assert d.main(["--apply"]) == 2
    assert "GRAFANA_API_TOKEN" in capsys.readouterr().out


def test_cli_check_passa_com_o_repo_em_dia(capsys):
    assert d.main(["--check"]) == 0
    assert "em dia" in capsys.readouterr().out


def test_cli_check_reprova_json_defasado(tmp_path, monkeypatch, capsys):
    (tmp_path / "visao-geral.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(d, "DASHBOARDS_DIR", tmp_path)
    assert d.main(["--check"]) == 1
    assert "make dashboards" in capsys.readouterr().out


def test_write_regenera_o_diretorio(tmp_path):
    escritos = d.write_all(tmp_path)
    assert len(escritos) == len(TODOS)
    for caminho in escritos:
        json.loads(caminho.read_text(encoding="utf-8"))  # JSON válido
    assert d.check_drift(tmp_path) == []
