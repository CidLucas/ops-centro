"""Testes do canal receiver→Hermes (RF07 — parte 2, issue #15).

As garantias que a issue pede, na ordem em que quebram na vida real:

1. o envelope é o contrato documentado, e a mensagem é MarkdownV2 válido (escape!);
2. Hermes fora do ar ⇒ retry com backoff e, esgotado, **dead-letter no Turso** — o
   critério de aceite "queda do Hermes não perde alerta silenciosamente";
3. tempestade de alerta ⇒ rate limit, com o número de suprimidas anunciado depois.
"""

import hashlib
import hmac
import json

import httpx
import libsql
import pytest

from ops_centro.receiver import hermes as h
from ops_centro.receiver.enrichment import AlertContext, EnrichedAlert
from ops_centro.turso.log_reader import LogLine
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture(autouse=True)
def limitador_limpo():
    """Balde de fichas é estado de processo — cada teste começa com o dele."""
    h.reset_limiter()
    yield
    h.reset_limiter()


def enriquecido(*, severity="warning", status="firing", logs=True):
    ctx = AlertContext(
        alertname="Agents Platform: taxa de erro por tool MCP acima de 10%",
        status=status,
        severity=severity,
        app_name="agents-platform",
        tenant_id="acme",
        trace_id=TRACE,
        environment="prod",
        summary="Tool search falhando em 23.4% das chamadas",
        description="Chamadas com status=error sobre o total.",
        runbook_url="https://exemplo/docs/alertas.md",
        labels={"tool": "search"},
    )
    linhas = (
        LogLine("2026-07-24T12:00:00.000+00:00", "agents-platform", "acme", TRACE, "ERROR",
                "tool search: upstream timeout (3/3)", '{"tool": "search"}'),
    ) if logs else ()
    return EnrichedAlert(ctx, linhas, strategy="trace" if logs else "nenhuma",
                         reason="" if logs else "TURSO_DATABASE_URL ausente")


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


def transporte(respostas):
    """AsyncClient que devolve as respostas dadas, em ordem, e conta as chamadas."""
    chamadas: list[httpx.Request] = []
    fila = list(respostas)

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        item = fila.pop(0) if len(fila) > 1 else fila[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, text="ok")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), chamadas


async def sem_espera(_segundos):
    """Substitui o sleep do backoff: o teste prova a sequência, não a duração."""
    return None


# --- contrato e formatação ------------------------------------------------------------
def test_envelope_carrega_o_contrato_versionado():
    notificacao = h.notification_from_alerts([enriquecido()])
    payload = notificacao.as_payload()
    assert payload["version"] == h.CONTRACT_VERSION
    assert payload["kind"] == h.KIND_ALERT
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["app_name"] == "agents-platform" and payload["tenant_id"] == "acme"
    assert payload["trace_id"] == TRACE
    # Os dados estruturados vão junto do texto: o Hermes pode reformatar se quiser.
    assert payload["alerts"][0]["logs"][0]["level"] == "ERROR"
    assert json.dumps(payload)  # serializável — é o corpo do POST


def test_mensagem_tem_emoji_de_severidade_e_link_clicavel(monkeypatch):
    monkeypatch.setenv("GRAFANA_STACK_URL", "https://exemplo.grafana.net")
    texto = h.render_markdown([enriquecido(severity="critical")])
    assert texto.startswith("🚨")
    assert "[dashboard](https://exemplo.grafana.net/d/" in texto
    assert "```" in texto  # log em bloco de código


def test_alerta_resolvido_troca_o_emoji():
    assert h.render_markdown([enriquecido(status="resolved")]).startswith("✅")


def test_escape_protege_os_reservados_do_markdown_v2():
    assert h.escape_md("erro (1.2) [x] _y_") == r"erro \(1\.2\) \[x\] \_y\_"
    # O que o parser do Telegram recusaria não pode sobrar cru no texto renderizado.
    ctx = AlertContext(alertname="p95 > 30s (agente: pesquisa_v2)", status="firing")
    texto = h.render_markdown([EnrichedAlert(ctx, (), reason="sem Turso")])
    assert r"\(agente: pesquisa\_v2\)" in texto


def test_sem_logs_a_mensagem_diz_o_motivo():
    texto = h.render_markdown([enriquecido(logs=False)])
    assert "Sem logs correlacionados" in texto


# --- entrega ---------------------------------------------------------------------------
async def test_entrega_no_primeiro_ok(monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_TOKEN", "segredo")
    notificacao = h.notification_from_alerts([enriquecido()])
    payload = notificacao.as_payload()
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client, chamadas = transporte([200])
    resultado = await h.deliver(notificacao,
                                client=client, url="https://hermes/notify")
    assert resultado.status == h.DELIVERED and resultado.attempts == 1
    assert json.loads(chamadas[0].content)["kind"] == h.KIND_ALERT
    # A assinatura cobre exatamente os bytes enviados (serializa → assina → envia).
    assert chamadas[0].content == corpo
    assinatura = hmac.new(b"segredo", chamadas[0].content, hashlib.sha256).hexdigest()
    assert chamadas[0].headers["x-hub-signature-256"] == f"sha256={assinatura}"
    assert chamadas[0].headers["x-hermes-token"] == "segredo"
    assert chamadas[0].headers["authorization"] == "Bearer segredo"


async def test_token_vai_nos_dois_formatos(monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_TOKEN", "segredo")
    client, chamadas = transporte([200])
    await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                    url="https://hermes/notify")
    assert chamadas[0].headers["x-hermes-token"] == "segredo"
    assert chamadas[0].headers["authorization"] == "Bearer segredo"


async def test_sem_token_auth_headers_vazias_e_post_sem_headers_de_auth(monkeypatch):
    monkeypatch.delenv("HERMES_WEBHOOK_TOKEN", raising=False)
    assert h._auth_headers(b"qualquer corpo") == {}
    client, chamadas = transporte([200])
    await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                    url="https://hermes/notify")
    assert "x-hermes-token" not in chamadas[0].headers
    assert "authorization" not in chamadas[0].headers
    assert "x-hub-signature-256" not in chamadas[0].headers


async def test_sem_url_configurada_vira_no_op(monkeypatch):
    monkeypatch.delenv("HERMES_WEBHOOK_URL", raising=False)
    resultado = await h.deliver(h.notification_from_alerts([enriquecido()]))
    assert resultado.status == h.DISABLED


async def test_erro_5xx_e_retentado_e_depois_entrega():
    client, chamadas = transporte([503, 200])
    resultado = await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                                url="https://hermes/notify", sleep=sem_espera)
    assert resultado.status == h.DELIVERED and resultado.attempts == 2
    assert len(chamadas) == 2


async def test_erro_4xx_nao_e_retentado(conectar):
    """Token errado não melhora na terceira tentativa — vai direto para o dead-letter."""
    client, chamadas = transporte([401])
    resultado = await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                                url="https://hermes/notify", connect_fn=conectar,
                                sleep=sem_espera)
    assert len(chamadas) == 1
    assert resultado.status == h.DEAD_LETTER and resultado.dead_lettered


async def test_hermes_fora_do_ar_nao_perde_o_alerta(conectar, banco):
    """Critério de aceite do #15: esgotado o retry, o envelope inteiro fica registrado."""
    client, chamadas = transporte([httpx.ConnectError("recusado")])
    resultado = await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                                url="https://hermes/notify", retries=3, connect_fn=conectar,
                                sleep=sem_espera)
    assert len(chamadas) == 3
    assert resultado.status == h.DEAD_LETTER and resultado.dead_lettered

    conn = libsql.connect(database=banco)
    linhas = conn.execute(
        "SELECT kind, attempts, reason, payload FROM hermes_dead_letter"
    ).fetchall()
    conn.close()
    assert len(linhas) == 1
    kind, tentativas, motivo, payload = linhas[0]
    assert (kind, tentativas) == (h.KIND_ALERT, 3)
    assert "recusado" in motivo
    # Payload guardado é reenviável: o contrato inteiro, não um resumo.
    assert json.loads(payload)["alerts"][0]["trace_id"] == TRACE


async def test_dead_letter_sem_turso_nao_levanta(monkeypatch, caplog):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    client, _ = transporte([500])
    resultado = await h.deliver(h.notification_from_alerts([enriquecido()]), client=client,
                                url="https://hermes/notify", retries=1, sleep=sem_espera)
    assert resultado.status == h.DEAD_LETTER and not resultado.dead_lettered
    assert "notificação perdida" in caplog.text


# --- rate limit -------------------------------------------------------------------------
async def test_rate_limit_segura_a_tempestade_e_anuncia_o_que_segurou(monkeypatch):
    monkeypatch.setenv("HERMES_RATE_LIMIT", "2")
    monkeypatch.setenv("HERMES_RATE_WINDOW", "3600")
    h.reset_limiter()
    client, chamadas = transporte([200])
    notificacao = h.notification_from_alerts([enriquecido()])

    desfechos = [
        (await h.deliver(notificacao, client=client, url="https://hermes/notify")).status
        for _ in range(4)
    ]
    assert desfechos == [h.DELIVERED, h.DELIVERED, h.SUPPRESSED, h.SUPPRESSED]
    assert len(chamadas) == 2

    # A ficha volta: a próxima mensagem entregue conta as que ficaram pelo caminho.
    h.get_limiter()._tokens = 1
    resultado = await h.deliver(notificacao, client=client, url="https://hermes/notify")
    assert resultado.status == h.DELIVERED and resultado.suppressed == 2
    assert "2 notificação" in json.loads(chamadas[-1].content)["text"]


def test_balde_de_fichas_recarrega_no_tempo():
    limitador = h.RateLimiter(capacity=2, window=10)
    assert [limitador.take()[0] for _ in range(3)] == [True, True, False]
    limitador._updated -= 10  # dez segundos depois
    assert limitador.take() == (True, 1)


# --- exemplo da CLI ------------------------------------------------------------------------
def test_exemplo_da_cli_e_um_envelope_valido():
    """O `make hermes-send` do teste ponta a ponta manda exatamente esta forma."""
    payload = h._exemplo().as_payload()
    assert payload["version"] == h.CONTRACT_VERSION
    assert payload["text"].startswith("⚠️")
    assert payload["alerts"][0]["logs"]
