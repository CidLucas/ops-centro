"""Testes dos alertas as-code (issue #12).

Três garantias, na ordem em que um alerta erra na vida real:

1. **Deriva** — o YAML commitado em `grafana/alerts/` é o que o gerador produz hoje.
2. **Correção da regra** — métrica que não existe, `histogram_quantile` sem `le` e divisão
   sem piso não dão erro no Grafana: dão regra que nunca dispara (ou que vive em estado de
   erro), que é a pior falha possível num alerta.
3. **Segredo fora do repo** — o YAML carrega placeholder, nunca o token (RNF06).

As invariantes de cada regra (uid, severity, histerese, `app_name`, métrica no catálogo,
`clamp_min`) **não** são testadas uma a uma aqui: elas moram em `alerts.validate_rules()`,
e o que este arquivo faz é chamá-lo e provar que ele reprova de verdade. Reimplementá-las
em teste parametrizado duplicaria a lógica e daria um relatório pior — o `validate_rules`
lista todas as violações de uma vez, com uid e motivo.
"""

import httpx
import pytest
import yaml

from ops_centro.conventions import ATTR_APP_NAME, ATTR_TENANT_ID
from ops_centro.grafana import alerts as a
from ops_centro.grafana.dashboards import FOLDER_TITLE, FOLDER_UID, GrafanaAPI

pytestmark = pytest.mark.unit

GRUPOS = a.build_groups()
REGRAS = a.all_rules()


# --- deriva ---------------------------------------------------------------------
def test_yaml_commitado_esta_em_dia_com_o_gerador():
    """Se este teste falhar: `make alerts` e commite o YAML junto da mudança."""
    assert a.check_drift() == []


def test_todos_os_arquivos_existem_no_repo():
    assert set(a.build_files()) == {
        "apps.yaml",
        "free-tier.yaml",
        "turso-retencao.yaml",
        "host.yaml",
        "roteamento.yaml",
    }
    for nome in a.build_files():
        assert (a.ALERTS_DIR / nome).exists()


def test_write_regenera_o_diretorio(tmp_path):
    escritos = a.write_all(tmp_path)
    assert len(escritos) == len(a.build_files())
    for caminho in escritos:
        yaml.safe_load(caminho.read_text(encoding="utf-8"))  # YAML válido
    assert a.check_drift(tmp_path) == []


def test_render_preserva_o_payload():
    """O cabeçalho é comentário: o que o Grafana lê tem de ser exatamente o payload."""
    grupo = a.build_apps()
    payload = grupo.as_provisioning()
    assert yaml.safe_load(a.render(payload, grupo.header)) == payload


# --- invariantes das regras --------------------------------------------------------
def test_regras_passam_na_validacao():
    """As invariantes de toda regra moram no `validate_rules`, não aqui: uid único e com
    prefixo, severity conhecida, `for` presente, summary/description, runbook clicável,
    `app_name` chegando ao alerta (#14) e métrica dentro do catálogo da §7.

    Testar isso regra a regra por fora do módulo seria reimplementar a validação — e pior:
    o `validate_rules` relata **todas** as violações de uma vez, com o uid e o motivo, o
    que um teste parametrizado não faz. O par deste teste é o de baixo, que prova que a
    validação reprova de verdade.
    """
    assert a.validate_rules() == []


def test_validacao_pega_violacao_injetada():
    """`validate_rules() == []` só vale alguma coisa se ele souber dizer não."""
    ruim = a.Rule(
        uid="regra-solta",  # sem o prefixo ops-centro-
        title="teste",
        # métrica com prefixo válido mas fora do catálogo + divisão sem clamp_min
        expr="sum(agents_platform_coisa_inventada_total) / sum(agents_platform_outra_total)",
        op="ge",  # operador não suportado
        threshold=1,
        duration="",  # sem histerese
        severity="urgentissimo",  # fora do vocabulário
        summary="",
        description="",
        component="apps",
    )
    problemas = " | ".join(a.validate_rules((ruim, ruim)))  # duplicado, de propósito
    for esperado in ("uid duplicado", "uid deve começar", "severity", "sem `for`",
                     "summary/description", "operador", "sem `app_name`",
                     "fora do catálogo", "clamp_min"):
        assert esperado in problemas, f"validação não pegou: {esperado}"


def test_todo_runbook_e_um_link_clicavel():
    """`runbook_url` é derivado do componente: basta um destes virar caminho relativo para
    a regra chegar ao Telegram mandando quem foi acordado procurar o arquivo sozinho."""
    for url in (a.RUNBOOK_ALERTAS, a.RUNBOOK_RETENCAO, a.RUNBOOK_FREE_TIER):
        assert url.startswith("https://")


def test_as_regras_dos_apps_cobrem_os_tres_sinais_da_issue():
    """Taxa de erro, latência p95 e falha de ingestão — as três famílias pedidas no #12."""
    exprs = [regra.expr for regra in a.build_apps().rules]
    assert any("agents_platform_agent_executions_total" in e for e in exprs)  # taxa de erro
    assert any("histogram_quantile" in e for e in exprs)  # p95
    assert any("context_mcp_ingestion_stage_total" in e for e in exprs)  # ingestão


def test_alerta_de_tenant_traz_o_tenant_na_label():
    regra = next(r for r in REGRAS if r.uid == "ops-centro-tenant-erro")
    assert ATTR_TENANT_ID in regra.grouped_labels()


def test_free_tier_usa_o_datasource_de_uso():
    """As métricas de billing não vivem no Prometheus do stack (docs/free-tier-baseline.md)."""
    for regra in a.build_free_tier().rules:
        assert regra.datasource == a.DS_USAGE
        assert a.referenced_metrics(regra) <= set(a.USAGE_METRICS)


def test_regra_de_retencao_parada_alerta_na_ausencia_de_dado():
    """A regra que vigia as outras duas: aqui 'sem dado' é o sintoma, não a exceção."""
    regra = next(r for r in REGRAS if r.uid == "ops-centro-retencao-parada")
    assert regra.no_data == "Alerting"
    assert regra.op == "lt"


def test_grupo_host_tem_cinco_regras_com_job_de_host():
    """As regras de host leem a integração Unix (`job="integrations/unix"`, issue #26); a
    do dead-man's switch (issue #27) vigia o heartbeat do próprio receiver e não passa pela
    integração Unix."""
    grupo = a.build_host()
    assert grupo.file == "host.yaml"
    assert grupo.name == "ops-centro-host"
    assert len(grupo.rules) == 5
    com_job = [r for r in grupo.rules if r.uid != "ops-centro-host-deadman"]
    assert len(com_job) == 4
    for regra in com_job:
        assert 'job="integrations/unix"' in regra.expr
    deadman = next(r for r in grupo.rules if r.uid == "ops-centro-host-deadman")
    assert "ops_centro_heartbeat_total" in deadman.expr


def test_restart_loop_usa_changes_no_estado_failed():
    """O loop de restart é detectado por transições failed↔activating, não por uma métrica
    de restarts que o node_exporter embutido no Alloy não expõe."""
    regra = next(r for r in REGRAS if r.uid == "ops-centro-host-restart-loop")
    assert "changes(" in regra.expr
    assert 'state="failed"' in regra.expr
    assert regra.severity == a.SEVERITY_CRITICAL


def test_coletor_parado_alerta_na_ausencia_de_sinal():
    """Mini dead-man's switch: se o Alloy sumir, `up` vira 0 ou NoData — e os dois são
    o sintoma, então NoData também alerta (o #27 completo é outra issue)."""
    regra = next(r for r in REGRAS if r.uid == "ops-centro-host-coletor-parado")
    assert regra.no_data == "Alerting"
    assert regra.op == "lt"


def test_deadman_alerta_na_ausencia_de_sinal():
    """Dead-man's switch do #27: a série do heartbeat some (NoData) ou congela
    (increase=0) — os dois são o sintoma. O `or vector(0)` é o que faz o modo 'congelou'
    alertar: `sum(increase(...))` sozinho devolveria NoData nos dois casos."""
    regra = next(r for r in REGRAS if r.uid == "ops-centro-host-deadman")
    assert regra.no_data == "Alerting"
    assert regra.component == "ec2-host"
    assert regra.duration == "10m"
    assert regra.op == "lt"
    assert regra.severity == a.SEVERITY_CRITICAL
    assert "sum(increase(ops_centro_heartbeat_total[10m]))" in regra.expr
    assert "or vector(0)" in regra.expr


def test_rota_do_25_entrega_o_deadman_fora_da_ec2():
    """A regra reusa a rota do #25: qualquer alerta com `component=ec2-host` (o do deadman
    entre eles) vai ao Telegram nativo do Grafana Cloud — que não depende da EC2 que parou
    de falar. Não alteramos o roteamento aqui: só conferimos que a regra cai nele."""
    policy = a.build_policy()
    infra = policy["routes"][0]
    assert infra["receiver"] == a.TELEGRAM_NAME
    assert ["component", "=", "ec2-host"] in infra["object_matchers"]
    deadman = next(r for r in REGRAS if r.uid == "ops-centro-host-deadman")
    assert deadman.component == "ec2-host"


def test_estrutura_do_provisionamento():
    for grupo in GRUPOS:
        payload = grupo.as_provisioning()
        assert payload["apiVersion"] == 1
        (bloco,) = payload["groups"]
        assert bloco["folder"] == FOLDER_TITLE
        for regra in bloco["rules"]:
            refs = [no["refId"] for no in regra["data"]]
            assert regra["condition"] in refs  # condição apontando para nó inexistente = regra morta
            assert regra["data"][0]["model"]["expr"].strip()


# --- roteamento --------------------------------------------------------------------
def test_roteamento_tem_os_dois_contact_points():
    """Webhook (receiver) + Telegram nativo — a rota de infra não passa pela EC2 (#25)."""
    contact_points = a.build_routing()["contactPoints"]
    assert len(contact_points) == 2
    assert {cp["name"] for cp in contact_points} == {a.RECEIVER_NAME, a.TELEGRAM_NAME}


def test_contact_point_aponta_para_o_receiver_com_o_token():
    receiver = a.build_contact_point()["receivers"][0]
    assert receiver["type"] == "webhook"
    assert receiver["settings"]["url"] == "${RECEIVER_WEBHOOK_URL}"
    assert receiver["settings"]["httpMethod"] == "POST"
    # Os dois formatos que o receiver aceita (ver ops_centro/receiver/app.py).
    assert receiver["settings"]["headers"]["X-Alert-Token"] == "${ALERT_WEBHOOK_TOKEN}"
    assert receiver["settings"]["authorization_credentials"] == "${ALERT_WEBHOOK_TOKEN}"


def test_telegram_usa_placeholder_para_bot_e_chat():
    """O contact point Telegram só carrega placeholder — o token do bot nunca vai ao repo."""
    contact_point = a.build_telegram_contact_point()
    assert contact_point["name"] == a.TELEGRAM_NAME
    (receiver,) = contact_point["receivers"]
    assert receiver["uid"] == a.TELEGRAM_UID
    assert receiver["type"] == "telegram"
    assert receiver["disableResolveMessage"] is False
    assert receiver["settings"]["bottoken"] == "${TELEGRAM_BOT_TOKEN}"
    assert receiver["settings"]["chatid"] == "${TELEGRAM_CHAT_ID}"


def test_policy_agrupa_por_app_e_tenant():
    """Sem agrupamento, um incidente que atinge 40 tenants vira 40 mensagens."""
    policy = a.build_policy()
    assert policy["receiver"] == a.RECEIVER_NAME
    assert ATTR_APP_NAME in policy["group_by"]
    assert ATTR_TENANT_ID in policy["group_by"]
    infra, critical = policy["routes"]
    # A rota de infra vem primeiro: alerta de host critical também casaria na rota de
    # severity=critical, e o Grafana usa a primeira rota que casa.
    assert infra["receiver"] == a.TELEGRAM_NAME
    assert ["component", "=", "ec2-host"] in infra["object_matchers"]
    assert infra["continue"] is False
    assert critical["receiver"] == a.RECEIVER_NAME
    assert critical["object_matchers"] == [["severity", "=", a.SEVERITY_CRITICAL]]


def test_nenhum_segredo_no_repo():
    """RNF06: os arquivos só carregam placeholder — o valor vem do ambiente no --apply."""
    conteudo = a.build_files()[a.ROUTING_FILE]
    assert "${ALERT_WEBHOOK_TOKEN}" in conteudo
    assert "${RECEIVER_WEBHOOK_URL}" in conteudo
    assert "${TELEGRAM_BOT_TOKEN}" in conteudo
    assert "${TELEGRAM_CHAT_ID}" in conteudo
    for suspeito in ("glsa_", "glc_", "Bearer glsa"):
        assert suspeito not in conteudo


def test_expand_resolve_os_placeholders():
    valores = {"ALERT_WEBHOOK_TOKEN": "segredo", "RECEIVER_WEBHOOK_URL": "https://x/y"}
    resolvido = a.expand(a.build_contact_point(), valores)
    settings = resolvido["receivers"][0]["settings"]
    assert settings["url"] == "https://x/y"
    assert settings["headers"]["X-Alert-Token"] == "segredo"


def test_expand_resolve_os_placeholders_do_telegram():
    valores = {"TELEGRAM_BOT_TOKEN": "123456:segredo", "TELEGRAM_CHAT_ID": "-100123"}
    resolvido = a.expand(a.build_telegram_contact_point(), valores)
    settings = resolvido["receivers"][0]["settings"]
    assert settings["bottoken"] == "123456:segredo"
    assert settings["chatid"] == "-100123"


def test_expand_preserva_placeholder_desconhecido():
    assert a.expand("${NAO_EXISTE}", {}) == "${NAO_EXISTE}"


# --- publicação ---------------------------------------------------------------------
VALORES = {
    "DS_PROM": "grafanacloud-prom",
    "DS_USAGE": "grafanacloud-usage",
    "RECEIVER_WEBHOOK_URL": "https://ops.exemplo.com/alerts/grafana",
    "ALERT_WEBHOOK_TOKEN": "segredo",
    "TELEGRAM_BOT_TOKEN": "123456:bot-segredo",
    "TELEGRAM_CHAT_ID": "-100123456789",
}


class FakeAPI(a.AlertingAPI):
    """Cliente que registra as chamadas em vez de fazê-las."""

    def __init__(self, contact_point_existe: bool = True):
        super().__init__(GrafanaAPI("https://exemplo.grafana.net", "glsa_fake"), dict(VALORES))
        self.chamadas: list[tuple[str, str, dict]] = []
        self.contact_point_existe = contact_point_existe
        self.api._put = self._put  # type: ignore[method-assign]
        self.api._post = self._post  # type: ignore[method-assign]
        self.api._get = lambda path: httpx.Response(  # type: ignore[method-assign]
            200, json={}, request=httpx.Request("GET", path)
        )

    def _put(self, path, payload):
        self.chamadas.append(("PUT", path, payload))
        existe = self.contact_point_existe or "contact-points" not in path
        return httpx.Response(
            200 if existe else 404, json={}, request=httpx.Request("PUT", path)
        )

    def _post(self, path, payload):
        self.chamadas.append(("POST", path, payload))
        return httpx.Response(200, json={"uid": FOLDER_UID}, request=httpx.Request("POST", path))


def test_aplicacao_publica_contact_point_grupos_e_policy():
    api = FakeAPI()
    assert a.apply_all(api) == 0
    caminhos = [caminho for _, caminho, _ in api.chamadas]
    assert f"/api/v1/provisioning/contact-points/{a.RECEIVER_UID}" in caminhos
    assert f"/api/v1/provisioning/contact-points/{a.TELEGRAM_UID}" in caminhos
    for grupo in GRUPOS:
        assert f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{grupo.name}" in caminhos
    assert "/api/v1/provisioning/policies" in caminhos


def test_aplicacao_e_idempotente_no_payload():
    """Duas execuções mandam exatamente o mesmo corpo — nada de versão embutida."""
    primeira, segunda = FakeAPI(), FakeAPI()
    a.apply_all(primeira)
    a.apply_all(segunda)
    assert primeira.chamadas == segunda.chamadas


def test_grupo_vai_com_folder_e_intervalo_em_segundos():
    api = FakeAPI()
    a.apply_all(api)
    corpo = next(c for _, caminho, c in api.chamadas if "rule-groups/ops-centro-apps" in caminho)
    assert corpo["folderUid"] == FOLDER_UID
    assert corpo["interval"] == 60  # a API fala em segundos; o arquivo, em "1m"
    for regra in corpo["rules"]:
        assert regra["folderUID"] == FOLDER_UID
        assert regra["ruleGroup"] == "ops-centro-apps"
        assert regra["isPaused"] is False
        assert "${" not in regra["data"][0]["datasourceUid"]  # placeholder resolvido


def test_contact_point_novo_cai_para_post():
    """PUT em uid inexistente é 404 — a primeira publicação num stack zerado passa por aqui."""
    api = FakeAPI(contact_point_existe=False)
    assert a.apply_all(api) == 0
    metodos = [(metodo, caminho) for metodo, caminho, _ in api.chamadas]
    assert ("POST", "/api/v1/provisioning/contact-points") in metodos


def test_skip_policy_nao_toca_na_arvore_de_roteamento(capsys):
    api = FakeAPI()
    assert a.apply_all(api, skip_policy=True) == 0
    assert "/api/v1/provisioning/policies" not in [caminho for _, caminho, _ in api.chamadas]
    assert "SKIP" in capsys.readouterr().out


def test_sem_segredo_no_ambiente_o_apply_reprova(capsys):
    api = FakeAPI()
    api.valores["ALERT_WEBHOOK_TOKEN"] = ""
    assert a.apply_all(api) == 2
    assert "ALERT_WEBHOOK_TOKEN" in capsys.readouterr().out
    assert api.chamadas == []  # nada publicado antes de reprovar


def test_falha_na_api_explica_a_permissao(capsys):
    class SemPermissao(FakeAPI):
        def _put(self, path, payload):
            self.chamadas.append(("PUT", path, payload))
            return httpx.Response(
                403,
                json={"message": "Permissions needed: alert.rules:write"},
                request=httpx.Request("PUT", path),
            )

    assert a.apply_all(SemPermissao()) == 1
    saida = capsys.readouterr().out
    assert "alert.rules:write" in saida
    assert "GRAFANA_API_TOKEN" in saida  # diz onde arrumar, não só o que faltou


# --- CLI ------------------------------------------------------------------------------
def test_cli_check_passa_com_o_repo_em_dia(capsys):
    assert a.main(["--check"]) == 0
    assert "em dia" in capsys.readouterr().out


def test_cli_check_reprova_yaml_defasado(tmp_path, monkeypatch, capsys):
    (tmp_path / "apps.yaml").write_text("apiVersion: 1\n", encoding="utf-8")
    monkeypatch.setattr(a, "ALERTS_DIR", tmp_path)
    assert a.main(["--check"]) == 1
    assert "make alerts" in capsys.readouterr().out


def test_cli_list_mostra_limiar_de_cada_regra(capsys):
    assert a.main(["--list"]) == 0
    saida = capsys.readouterr().out
    assert "ops-centro-agents-erro-execucao" in saida
    assert "ops-centro-free-tier" in saida


def test_cli_sem_credencial_sai_com_erro(monkeypatch, capsys):
    for var in ("GRAFANA_API_TOKEN", "GRAFANA_READ_TOKEN", "GRAFANA_STACK_URL"):
        monkeypatch.delenv(var, raising=False)
    assert a.main(["--apply"]) == 2
    assert "GRAFANA_STACK_URL" in capsys.readouterr().out
