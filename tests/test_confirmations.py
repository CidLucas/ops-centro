"""Testes do fluxo de confirmação das ações de maior impacto (RF10, issue #18).

Os dois critérios de aceite da issue, traduzidos em testes:

1. restart **proposto, confirmado e executado** de ponta a ponta, com relato no Telegram
   (`test_restart_proposto_confirmado_e_executado`);
2. confirmação **vencida** ou vinda de **chat não autorizado** é recusada — e fica logada
   no audit (`test_confirmacao_vencida_*`, `test_chat_nao_autorizado_*`).

O resto do arquivo é sobre as maneiras de **não** executar: token desconhecido, token já
usado, outro chat, ação fora da allowlist. Numa ação que reinicia produção, a lista de
recusas é o produto.
"""

import json
from datetime import timedelta

import httpx
import libsql
import pytest

from ops_centro.receiver import confirmations as c
from ops_centro.turso import audit
from ops_centro.turso import confirmations as store
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit

CHAT = "-1001234"
AGORA = audit.utcnow()


@pytest.fixture(autouse=True)
def ambiente(monkeypatch):
    """Um chat autorizado e um endpoint admin — o mínimo para a ação existir."""
    monkeypatch.setenv("HERMES_ALLOWED_CHAT_IDS", f"{CHAT}, 999")
    monkeypatch.setenv("ADMIN_API_AGENTS_PLATFORM_URL", "https://agents.exemplo/admin-api")
    monkeypatch.setenv("ADMIN_API_TOKEN", "segredo")


@pytest.fixture
def banco(tmp_path):
    caminho = str(tmp_path / "logs.db")
    conn = libsql.connect(database=caminho)
    apply_migrations(conn)
    conn.close()
    return caminho


@pytest.fixture
def conectar(banco):
    return lambda: libsql.connect(database=banco)


def admin_falso(status_code=200):
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        return httpx.Response(status_code, json={"ok": status_code < 300})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), chamadas


def acoes(banco, limit=20):
    conn = libsql.connect(database=banco)
    try:
        return audit.recent_actions(conn, limit=limit)
    finally:
        conn.close()


async def propor(conectar, acao="restart_service", alvo="agents-platform", **kwargs):
    return await c.propose(acao, alvo, user="lucas", chat_id=CHAT, connect_fn=conectar, **kwargs)


# --- proposta ---------------------------------------------------------------------------
async def test_proposta_traz_impacto_e_dois_botoes(conectar, banco):
    proposta = await propor(conectar)

    assert proposta.ok and proposta.token
    # O impacto vai na mensagem: botão sem a frase que explica o estrago é só um caminho
    # mais curto para o mesmo acidente.
    assert "requisições em voo se perdem" in proposta.text.replace("\\", "")
    assert "Vale por 10 min" in proposta.text.replace("\\", "")
    assert [botao["text"] for botao in proposta.buttons[0]] == ["✅ Confirmar", "✖️ Cancelar"]
    assert c.parse_callback(proposta.buttons[0][0]["callback_data"]) == (
        c.DECISION_CONFIRM, proposta.token
    )
    # E a proposta já é auditoria (#19), antes de qualquer execução.
    registro = acoes(banco)[0]
    assert (registro.action, registro.status) == ("restart_service", audit.STATUS_PROPOSED)
    assert registro.actor == "telegram:lucas"


async def test_token_nao_e_guardado_em_claro(conectar, banco):
    """Quem lê o banco não consegue confirmar nada em nome de ninguém."""
    proposta = await propor(conectar)
    conn = libsql.connect(database=banco)
    linhas = conn.execute("SELECT token_hash FROM action_confirmations").fetchall()
    conn.close()
    assert linhas[0][0] == store.token_hash(proposta.token) != proposta.token


async def test_acao_fora_da_allowlist_nao_vira_proposta(conectar, banco):
    proposta = await c.propose("rm_-rf", "/", user="lucas", chat_id=CHAT, connect_fn=conectar)
    assert not proposta.ok and proposta.detail == c.REJECT_NOT_CONFIRMABLE
    assert acoes(banco)[0].status == audit.STATUS_BLOCKED


async def test_restart_so_aceita_os_apps_conhecidos(conectar, banco):
    """`target` vem do Telegram: alvo livre aqui seria reiniciar o que o admin_api aceitar."""
    proposta = await propor(conectar, alvo="banco-de-producao")
    assert not proposta.ok and "alvo inválido" in proposta.detail


async def test_despausar_sem_pausa_vigente_nao_propoe(conectar, banco):
    proposta = await propor(conectar, acao="resume_tool", alvo="search")
    assert not proposta.ok and "nenhuma pausa vigente" in proposta.detail


async def test_despausar_herda_o_app_da_pausa(conectar, banco):
    conn = libsql.connect(database=banco)
    audit.record_action(
        conn,
        audit.ActionRecord(
            action=audit.ACTION_PAUSE_TOOL, target="search", status=audit.STATUS_OK,
            app_name="agents-platform", ttl_seconds=900,
            expires_at=audit.expires_at(900, AGORA),
        ),
        now=AGORA,
    )
    conn.close()
    proposta = await propor(conectar, acao="resume_tool", alvo="search", now=AGORA)
    assert proposta.ok and proposta.app_name == "agents-platform"


# --- confirmação: o caminho feliz ------------------------------------------------------------
async def test_restart_proposto_confirmado_e_executado(conectar, banco):
    """O critério de aceite inteiro: propõe → confirma → executa → relata."""
    proposta = await propor(conectar)
    client, chamadas = admin_falso()

    resultado = await c.confirm(
        callback=proposta.buttons[0][0]["callback_data"], user="lucas", chat_id=CHAT,
        connect_fn=conectar, client=client,
    )

    assert resultado.status == audit.STATUS_OK and resultado.executed
    # 1. o admin_api do app foi chamado, autenticado, com o motivo
    assert chamadas[0].url.path.endswith("/admin/service/restart")
    assert chamadas[0].headers["authorization"] == "Bearer segredo"
    assert "telegram:lucas" in json.loads(chamadas[0].read())["reason"]
    # 2. o relato volta no mesmo thread da proposta
    resposta = resultado.as_response(message_id=42)
    assert resposta["reply_to_message_id"] == 42
    assert "executado" in resposta["text"] and "telegram:lucas" in resposta["text"]
    # 3. proposta e execução, nessa ordem, no audit (#19)
    assert [(a.action, a.status) for a in acoes(banco)] == [
        ("restart_service", audit.STATUS_OK),
        ("restart_service", audit.STATUS_PROPOSED),
    ]


async def test_cancelar_nao_executa_nada(conectar, banco):
    proposta = await propor(conectar)
    client, chamadas = admin_falso()

    resultado = await c.confirm(
        callback=proposta.buttons[0][1]["callback_data"], user="lucas", chat_id=CHAT,
        connect_fn=conectar, client=client,
    )

    assert resultado.status == audit.STATUS_CANCELLED and not resultado.executed
    assert chamadas == []
    assert acoes(banco)[0].status == audit.STATUS_CANCELLED


async def test_falha_do_admin_api_vira_error_auditado(conectar, banco):
    proposta = await propor(conectar)
    client, _ = admin_falso(status_code=500)
    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="lucas", chat_id=CHAT,
        connect_fn=conectar, client=client,
    )
    assert resultado.status == audit.STATUS_ERROR and not resultado.executed
    assert "HTTP 500" in acoes(banco)[0].detail
    assert "falhou" in resultado.text


async def test_sem_endpoint_admin_a_execucao_e_bloqueada(conectar, banco, monkeypatch):
    proposta = await propor(conectar)
    monkeypatch.delenv("ADMIN_API_AGENTS_PLATFORM_URL", raising=False)
    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="lucas", chat_id=CHAT, connect_fn=conectar
    )
    # Nada foi chamado: é configuração faltando, não erro do app.
    assert resultado.status == audit.STATUS_BLOCKED
    assert acoes(banco)[0].detail == c.BLOCK_NO_ADMIN


# --- confirmação: as recusas -------------------------------------------------------------------
async def test_confirmacao_vencida_nao_executa_e_fica_logada(conectar, banco):
    """Critério de aceite: um 'sim' de dez minutos atrás não reinicia nada agora."""
    proposta = await propor(conectar, ttl_seconds=600, now=AGORA)
    client, chamadas = admin_falso()

    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="lucas", chat_id=CHAT,
        connect_fn=conectar, client=client, now=AGORA + timedelta(minutes=11),
    )

    assert resultado.status == audit.STATUS_BLOCKED and resultado.detail == c.REJECT_EXPIRED
    assert chamadas == []
    assert acoes(banco)[0].detail == c.REJECT_EXPIRED
    assert "/reiniciar agents-platform" in resultado.text.replace("\\", "")


async def test_chat_nao_autorizado_nao_confirma_e_fica_logado(conectar, banco):
    proposta = await propor(conectar)
    client, chamadas = admin_falso()

    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="intruso", chat_id="777",
        connect_fn=conectar, client=client,
    )

    assert resultado.status == audit.STATUS_BLOCKED
    assert resultado.detail == c.REJECT_UNAUTHORIZED
    assert chamadas == []
    registro = acoes(banco)[0]
    assert registro.actor == "telegram:intruso" and registro.status == audit.STATUS_BLOCKED
    # O token nunca aparece no audit — só a impressão digital dele.
    assert proposta.token not in registro.target and registro.target.startswith("token:")


async def test_chat_autorizado_diferente_do_que_propos_nao_confirma(conectar, banco):
    """Token vazado de uma conversa não vale em outra, mesmo que a outra seja autorizada."""
    proposta = await propor(conectar)
    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="outro", chat_id="999",
        connect_fn=conectar,
    )
    assert resultado.detail == c.REJECT_OTHER_CHAT


async def test_token_de_uso_unico(conectar, banco):
    """Rede ruim no celular reenvia o mesmo callback — o restart acontece uma vez só."""
    proposta = await propor(conectar)
    client, chamadas = admin_falso()
    comum = dict(token=proposta.token, decision="confirm", user="lucas", chat_id=CHAT,
                 connect_fn=conectar, client=client)

    primeira = await c.confirm(**comum)
    segunda = await c.confirm(**comum)

    assert primeira.status == audit.STATUS_OK
    assert segunda.status == audit.STATUS_BLOCKED and segunda.detail == c.REJECT_USED
    assert len(chamadas) == 1


async def test_token_desconhecido_e_recusado(conectar, banco):
    resultado = await c.confirm(
        token="inventado", decision="confirm", user="lucas", chat_id=CHAT, connect_fn=conectar
    )
    assert resultado.detail == c.REJECT_UNKNOWN_TOKEN
    assert acoes(banco)[0].status == audit.STATUS_BLOCKED


async def test_callback_estranho_nao_e_interpretado(conectar):
    resultado = await c.confirm(callback="drop table", user="lucas", chat_id=CHAT,
                                connect_fn=conectar)
    assert resultado.detail == c.REJECT_NO_DECISION


async def test_allowlist_vazia_falha_fechada(conectar, monkeypatch):
    """Esquecer a env var não pode transformar o bot num botão público de restart."""
    monkeypatch.delenv("HERMES_ALLOWED_CHAT_IDS", raising=False)
    assert not c.chat_authorized(CHAT)
    proposta = await propor(conectar)
    assert not proposta.ok and proposta.detail == c.REJECT_UNAUTHORIZED


async def test_sem_turso_nao_ha_proposta(monkeypatch):
    """Sem onde guardar o token de uso único não existe confirmação confiável."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    proposta = await c.propose("restart_service", "agents-platform", user="lucas", chat_id=CHAT)
    assert not proposta.ok and proposta.detail == c.REJECT_NO_TURSO


async def test_kill_switch_das_acoes_autonomas_nao_bloqueia_o_humano(conectar, monkeypatch):
    """`AUTONOMOUS_ACTIONS=off` desliga o que o ops-centro faz sozinho, não quem confirma."""
    monkeypatch.setenv("AUTONOMOUS_ACTIONS", "off")
    proposta = await propor(conectar)
    client, chamadas = admin_falso()
    resultado = await c.confirm(
        token=proposta.token, decision="confirm", user="lucas", chat_id=CHAT,
        connect_fn=conectar, client=client,
    )
    assert resultado.status == audit.STATUS_OK and len(chamadas) == 1


# --- entrada pelo Telegram (comando → proposta → endpoint) -----------------------------------
async def test_comando_reiniciar_devolve_a_proposta_com_botoes(conectar, banco):
    from ops_centro.receiver.status import run_command

    resposta = await run_command(
        "/reiniciar agents-platform", connect_fn=conectar, chat_id=CHAT, user="lucas"
    )
    assert resposta["command"] == "reiniciar"
    assert resposta["data"]["proposed"] and resposta["buttons"]
    assert resposta["parse_mode"] == "MarkdownV2"


def test_endpoint_de_confirmacao_exige_o_token_do_hermes(monkeypatch):
    from fastapi.testclient import TestClient

    from ops_centro.receiver.app import app

    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    with TestClient(app) as client:
        corpo = {"callback_data": "ops:confirm:qualquer", "chat_id": CHAT, "user": "lucas"}
        assert client.post("/hermes/confirmacao", json=corpo).status_code == 401
        resp = client.post(
            "/hermes/confirmacao", json=corpo, headers={"X-Alert-Token": "segredo"}
        )
        # 200 mesmo recusando: um 4xx viraria "o bot não respondeu" para quem apertou.
        assert resp.status_code == 200
        assert resp.json()["status"] == audit.STATUS_BLOCKED
