"""Testes da ação autônoma de baixo risco (RF09, issue #17).

O critério de aceite da issue, traduzido em testes:

1. tool falhando de novo e de novo ⇒ pausa automática + mensagem no Telegram explicando
   **o quê** e **por quê** (`test_tool_com_falha_recorrente_e_pausada_e_anunciada`);
2. despausa automática depois do TTL, provada com o relógio nas mãos
   (`test_sweep_despausa_o_que_venceu_e_avisa`);
3. kill switch desliga tudo — e mesmo bloqueada, a ação fica auditada.

Ação automática errada custa mais que alerta perdido, então a maioria destes testes é
sobre **não agir**: sem evidência no Turso, sem endpoint admin ou com pausa vigente, nada
acontece.
"""

import json

import httpx
import libsql
import pytest

from ops_centro.receiver import actions as a
from ops_centro.receiver.enrichment import AlertContext, EnrichedAlert
from ops_centro.turso import audit
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
AGORA = audit.utcnow()


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch):
    """Kill switch ligado por default e URLs de admin explícitas em cada teste."""
    monkeypatch.setenv("AUTONOMOUS_ACTIONS", "on")
    monkeypatch.setenv("ADMIN_API_AGENTS_PLATFORM_URL", "https://agents.exemplo/admin-api")
    monkeypatch.setenv("ADMIN_API_TOKEN", "segredo")
    monkeypatch.delenv("HERMES_WEBHOOK_URL", raising=False)


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


def semear_falhas(banco, quantidade, *, tool="search", app="agents-platform", nivel="ERROR"):
    conn = libsql.connect(database=banco)
    conn.executemany(
        "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                audit._iso(AGORA), app, "acme", TRACE, nivel,
                f"tool {tool}: upstream timeout", '{"tool": "%s"}' % tool,
            )
            for _ in range(quantidade)
        ],
    )
    conn.commit()
    conn.close()


def alerta(tool="search", status="firing", app="agents-platform"):
    labels = {"app_name": app, "tenant_id": "acme", "severity": "warning"}
    if tool:
        labels["tool"] = tool
    ctx = AlertContext(
        alertname="Agents Platform: taxa de erro por tool MCP acima de 10%",
        status=status, severity="warning", app_name=app, tenant_id="acme", trace_id=TRACE,
        environment="prod", summary=f"Tool {tool} falhando", labels=labels,
    )
    return EnrichedAlert(ctx, (), strategy="trace")


def admin_falso(status_code=200):
    """admin_api falso: registra as chamadas de pause/resume."""
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        return httpx.Response(status_code, json={"ok": status_code < 300})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), chamadas


@pytest.fixture
def notificacoes(monkeypatch):
    """Intercepta o canal do #15 — aqui interessa o que foi dito, não como foi entregue."""
    enviadas = []

    async def falso_deliver(notification, **kwargs):
        enviadas.append(notification)
        from ops_centro.receiver.hermes import DELIVERED, DeliveryResult

        return DeliveryResult(DELIVERED, attempts=1)

    monkeypatch.setattr(a, "deliver", falso_deliver)
    return enviadas


def acoes_no_banco(banco):
    conn = libsql.connect(database=banco)
    try:
        return audit.recent_actions(conn, limit=20)
    finally:
        conn.close()


# --- decisão ---------------------------------------------------------------------------
async def test_falhas_acima_do_limiar_decidem_pausar(conectar, banco):
    semear_falhas(banco, 6)
    decisao = await a.decide(alerta(), connect_fn=conectar, threshold=5, window_minutes=30,
                             now=AGORA)
    assert decisao.act and decisao.tool == "search" and decisao.failures == 6
    assert "limiar 5" in decisao.reason


async def test_abaixo_do_limiar_nao_age(conectar, banco):
    semear_falhas(banco, 2)
    decisao = await a.decide(alerta(), connect_fn=conectar, threshold=5, now=AGORA)
    assert not decisao.act and decisao.failures == 2


async def test_alerta_sem_label_tool_nao_age(conectar):
    decisao = await a.decide(alerta(tool=""), connect_fn=conectar, now=AGORA)
    assert not decisao.act and "sem label" in decisao.reason


async def test_alerta_resolvido_nao_age(conectar, banco):
    semear_falhas(banco, 9)
    decisao = await a.decide(alerta(status="resolved"), connect_fn=conectar, now=AGORA)
    assert not decisao.act and "resolved" in decisao.reason


async def test_sem_evidencia_no_turso_nao_age(conectar):
    """Falha **fechada**: sem a contagem que prova a recorrência, nada é pausado."""
    def explode():
        raise RuntimeError("Turso fora do ar")

    decisao = await a.decide(alerta(), connect_fn=explode, now=AGORA)
    assert not decisao.act and "indisponível" in decisao.reason


async def test_falhas_de_outra_tool_nao_contam(conectar, banco):
    semear_falhas(banco, 8, tool="fetch")
    decisao = await a.decide(alerta(tool="search"), connect_fn=conectar, threshold=5, now=AGORA)
    assert not decisao.act and decisao.failures == 0


async def test_tool_ja_pausada_entra_em_cooldown(conectar, banco):
    semear_falhas(banco, 9)
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
    decisao = await a.decide(alerta(), connect_fn=conectar, threshold=5, now=AGORA)
    assert not decisao.act and a.BLOCK_COOLDOWN in decisao.reason


# --- execução ----------------------------------------------------------------------------
async def test_tool_com_falha_recorrente_e_pausada_e_anunciada(conectar, banco, notificacoes):
    """O critério de aceite inteiro, de ponta a ponta: alerta → pausa → mensagem."""
    semear_falhas(banco, 7)
    client, chamadas = admin_falso()

    executadas = await a.handle_alerts(
        [alerta()], connect_fn=conectar, client=client, now=AGORA
    )

    assert executadas and executadas[0]["status"] == audit.STATUS_OK
    # 1. o app foi chamado com tool, TTL e motivo
    corpo = json.loads(chamadas[0].read())
    assert chamadas[0].url.path.endswith("/admin/tools/pause")
    assert corpo["tool_name"] == "search" and corpo["ttl_seconds"] == 900
    assert "7 falhas" in corpo["reason"]
    assert chamadas[0].headers["authorization"] == "Bearer segredo"
    # 2. a ação ficou auditada, com o gatilho e o trace do alerta (#19)
    registro = acoes_no_banco(banco)[0]
    assert (registro.action, registro.target, registro.status) == (
        audit.ACTION_PAUSE_TOOL, "search", audit.STATUS_OK
    )
    assert registro.trace_id == TRACE and "7 falha" in registro.triggered_by
    assert registro.expires_at > audit._iso(AGORA)
    # 3. o Telegram foi avisado do quê e do porquê
    texto = notificacoes[0].text
    assert "search" in texto and "7 falhas" in texto
    assert "Despausa automática" in texto
    assert notificacoes[0].kind == "action"


async def test_kill_switch_bloqueia_e_ainda_assim_audita(conectar, banco, monkeypatch,
                                                         notificacoes):
    monkeypatch.setenv("AUTONOMOUS_ACTIONS", "off")
    semear_falhas(banco, 9)
    client, chamadas = admin_falso()

    assert await a.handle_alerts([alerta()], connect_fn=conectar, client=client) == []
    assert chamadas == [] and notificacoes == []

    # `handle_alerts` nem avalia com o switch desligado; a trilha aparece quando a ação
    # é pedida diretamente (o caminho que a #18 vai usar com confirmação humana).
    decisao = a.Decision(True, tool="search", app_name="agents-platform", failures=9)
    resultado = await a.pause_tool(decisao, connect_fn=conectar, client=client, now=AGORA)
    assert resultado.status == audit.STATUS_BLOCKED
    assert acoes_no_banco(banco)[0].detail == a.BLOCK_KILL_SWITCH
    assert chamadas == []


async def test_sem_endpoint_admin_a_acao_e_bloqueada(conectar, banco, monkeypatch,
                                                     notificacoes):
    monkeypatch.delenv("ADMIN_API_AGENTS_PLATFORM_URL", raising=False)
    decisao = a.Decision(True, tool="search", app_name="agents-platform", failures=9)
    resultado = await a.pause_tool(decisao, connect_fn=conectar, now=AGORA)
    assert resultado.status == audit.STATUS_BLOCKED
    assert acoes_no_banco(banco)[0].detail == a.BLOCK_NO_ADMIN
    assert notificacoes == []


async def test_falha_do_admin_api_vira_acao_com_status_error(conectar, banco, notificacoes):
    client, _ = admin_falso(status_code=500)
    decisao = a.Decision(True, tool="search", app_name="agents-platform", failures=9,
                         reason="9 falhas em 30min")
    resultado = await a.pause_tool(decisao, connect_fn=conectar, client=client, now=AGORA)
    assert resultado.status == audit.STATUS_ERROR
    assert "HTTP 500" in acoes_no_banco(banco)[0].detail
    # Mesmo falhando, o canal avisa: pausa que não aconteceu é informação operacional.
    assert notificacoes


async def test_uma_tool_por_webhook(conectar, banco, notificacoes):
    """Incidente que derruba três tools não é problema de tool — pausar três agrava."""
    semear_falhas(banco, 6, tool="search")
    semear_falhas(banco, 6, tool="fetch")
    client, chamadas = admin_falso()
    executadas = await a.handle_alerts(
        [alerta(tool="search"), alerta(tool="fetch")], connect_fn=conectar, client=client,
        now=AGORA,
    )
    assert len(executadas) == 1 and len(chamadas) == 1


# --- TTL e despausa ---------------------------------------------------------------------------
async def test_sweep_despausa_o_que_venceu_e_avisa(conectar, banco, notificacoes):
    from datetime import timedelta

    conn = libsql.connect(database=banco)
    audit.record_action(
        conn,
        audit.ActionRecord(
            action=audit.ACTION_PAUSE_TOOL, target="search", status=audit.STATUS_OK,
            app_name="agents-platform", ttl_seconds=900,
            expires_at=audit.expires_at(900, AGORA - timedelta(hours=1)),
        ),
        now=AGORA - timedelta(hours=1),
    )
    conn.close()
    client, chamadas = admin_falso()

    resultados = await a.sweep_expired(connect_fn=conectar, client=client, now=AGORA)

    assert [r.status for r in resultados] == [audit.STATUS_OK]
    assert chamadas[0].url.path.endswith("/admin/tools/resume")
    assert "despausada" in notificacoes[0].text
    # Idempotente: a linha de resume cancela a pausa, então a segunda passada não faz nada.
    assert await a.sweep_expired(connect_fn=conectar, client=client, now=AGORA) == []


async def test_pausa_ainda_vigente_nao_e_desfeita(conectar, banco, notificacoes):
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
    client, chamadas = admin_falso()
    assert await a.sweep_expired(connect_fn=conectar, client=client, now=AGORA) == []
    assert chamadas == []


async def test_sweep_sem_turso_nao_levanta(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    assert await a.sweep_expired() == []


def test_kill_switch_aceita_as_formas_usuais(monkeypatch):
    for valor in ("off", "0", "false", "no", "OFF"):
        monkeypatch.setenv("AUTONOMOUS_ACTIONS", valor)
        assert not a.actions_enabled()
    for valor in ("on", "1", "true", ""):
        monkeypatch.setenv("AUTONOMOUS_ACTIONS", valor)
        assert a.actions_enabled()
