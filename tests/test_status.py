"""Testes das consultas sob demanda (RF08, issue #16).

O que a issue exige e estes testes provam:

1. `/status` responde a saúde dos dois apps a partir de queries **agregadas** — e nenhuma
   delas varre trace no Tempo (o limite de custo é parte do contrato, não uma intenção);
2. os comandos são os que o Telegram manda de verdade, sufixo de bot incluso;
3. nada disso levanta: Grafana ou Turso fora deixam a resposta mais pobre, não quebrada.
"""

import httpx
import libsql
import pytest
from fastapi.testclient import TestClient

from ops_centro.conventions import APP_AGENTS_PLATFORM, APP_FILE_MEMORY
from ops_centro.metrics import BY_NAME
from ops_centro.receiver import status as s
from ops_centro.receiver.app import app
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit


class FakeEndpoint:
    """Prometheus falso: um valor por trecho de query reconhecido."""

    configured = True

    def __init__(self, por_trecho: dict[str, float] | None = None, status_code: int = 200):
        self.por_trecho = por_trecho or {}
        self.status_code = status_code
        self.queries: list[str] = []

    def get(self, path, params):
        self.queries.append(params["query"])
        chave = next((k for k in self.por_trecho if k in params["query"]), None)
        valor = self.por_trecho.get(chave, 0.0)
        return httpx.Response(
            self.status_code,
            json={"data": {"result": [{"metric": {}, "value": [0, str(valor)]}]}},
            request=httpx.Request("GET", "http://fake"),
        )


class FakeCloud:
    def __init__(self, prom):
        self.prom = prom


@pytest.fixture
def cloud():
    return FakeCloud(
        FakeEndpoint(
            {
                'status="error"': 12.0,  # numerador do erro %
                "increase(agents_platform_agent_executions_total": 400.0,
                "histogram_quantile(0.95, sum by (le) (rate(agents_platform": 8.5,
                "increase(context_mcp_tool_calls_total": 90.0,
                "histogram_quantile(0.95, sum by (le) (rate(context_mcp": 1.2,
            }
        )
    )


@pytest.fixture
def banco(tmp_path):
    caminho = str(tmp_path / "logs.db")
    conn = libsql.connect(database=caminho)
    apply_migrations(conn)
    conn.executemany(
        "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("2999-01-01T00:00:00.000+00:00", "agents-platform", "acme", None, "ERROR",
             "tool search estourou o timeout", '{"tool": "search"}'),
            ("2999-01-01T00:00:01.000+00:00", "agents-platform", "acme", None, "ERROR",
             "tool search de novo", '{"tool": "search"}'),
            ("2999-01-01T00:00:02.000+00:00", "file-memory-mcp", "beta", None, "CRITICAL",
             "ingestão travou", None),
            ("2999-01-01T00:00:03.000+00:00", "agents-platform", "acme", None, "INFO",
             "tudo certo", None),
        ],
    )
    conn.commit()
    conn.close()
    return caminho


@pytest.fixture
def conectar(banco):
    return lambda: libsql.connect(database=banco)


# --- comandos ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto, nome, argumento",
    [
        ("/status", s.CMD_STATUS, ""),
        ("status", s.CMD_STATUS, ""),
        ("/status acme", s.CMD_STATUS, "acme"),
        ("/status@hermes_bot acme", s.CMD_STATUS, "acme"),  # o Telegram faz isso em grupo
        ("/erros hoje", s.CMD_ERROS, "hoje"),
        ("/acoes", s.CMD_ACOES, ""),
        ("/ações", s.CMD_ACOES, ""),  # com acento é como se digita no celular
        ("/reiniciar agents-platform", s.CMD_REINICIAR, "agents-platform"),
        ("/despausar search", s.CMD_DESPAUSAR, "search"),
        ("/desligar tudo", s.CMD_AJUDA, "tudo"),
        ("", s.CMD_AJUDA, ""),
    ],
)
def test_parse_command(texto, nome, argumento):
    comando = s.parse_command(texto)
    assert (comando.name, comando.argument) == (nome, argumento)


@pytest.mark.parametrize(
    "argumento, janela",
    [("hoje", "24h"), ("30m", "30m"), ("", "1h"), ("ontem à tarde", "1h")],
)
def test_resolve_window(argumento, janela):
    assert s.resolve_window(argumento) == janela


# --- queries fechadas ---------------------------------------------------------------
def test_queries_so_citam_metricas_do_catalogo():
    for app_name in (APP_AGENTS_PLATFORM, APP_FILE_MEMORY):
        for query in s.build_queries(app_name).values():
            citadas = [nome for nome in BY_NAME if nome in query]
            assert citadas, f"query sem métrica do catálogo: {query}"


def test_queries_sao_agregadas_e_nao_tocam_o_tempo():
    """Limite de custo da issue: agregação pronta, nunca varredura livre de traces."""
    for query in s.build_queries(APP_AGENTS_PLATFORM).values():
        assert query.startswith(("sum(", "100 * sum(", "histogram_quantile("))
        # `by (le)` é o único agrupamento tolerado — qualquer outro multiplica séries lidas.
        assert "by (" not in query or "by (le)" in query
        assert "traceql" not in query and "tempo" not in query


def test_consulta_por_tenant_usa_o_counter_de_volume(monkeypatch):
    """`tenant_id` só existe nas métricas de volume (regra de cardinalidade do schema)."""
    queries = s.build_queries(APP_AGENTS_PLATFORM, tenant="acme")
    assert all("agents_platform_tenant_executions_total" in q for q in queries.values())
    assert all('tenant_id="acme"' in q for q in queries.values())
    assert "p95_s" not in queries


def test_filtro_de_ambiente_entra_em_toda_query(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert all('environment="prod"' in q for q in s.build_queries(APP_FILE_MEMORY).values())


# --- coleta -------------------------------------------------------------------------
def test_saude_do_app_vem_das_tres_series(cloud):
    saude = s.collect_app_health(cloud, APP_AGENTS_PLATFORM, window="1h")
    assert saude.volume == 400.0
    assert saude.error_pct == 12.0
    assert saude.p95_seconds == 8.5
    assert "🟡" in saude.line() and "p95 8.5s" in saude.line()


def test_prometheus_fora_do_ar_vira_sem_dados():
    saude = s.collect_app_health(
        FakeCloud(FakeEndpoint(status_code=500)), APP_AGENTS_PLATFORM, window="1h"
    )
    assert saude.volume is None and "não respondeu" in saude.detail
    assert "sem dados" in saude.line()


def test_erros_do_turso_sao_contados_no_banco(conectar):
    resumo = s.collect_errors(conectar, window="24h")
    assert resumo.total == 3
    assert ("agents-platform", "ERROR", 2) in resumo.counts
    assert len(resumo.lines) == 3  # INFO fica de fora


async def test_status_responde_com_texto_e_dados(cloud, conectar):
    resposta = await s.run_command("/status", cloud=cloud, connect_fn=conectar)
    assert resposta["command"] == s.CMD_STATUS
    assert resposta["parse_mode"] == "MarkdownV2"
    assert "Agents Platform" in resposta["text"] and "File Memory" in resposta["text"]
    assert [app["app"] for app in resposta["data"]["apps"]] == [
        APP_AGENTS_PLATFORM, APP_FILE_MEMORY
    ]


async def test_status_por_tenant_avisa_que_nao_ha_p95(cloud, conectar):
    resposta = await s.run_command("/status acme", cloud=cloud, connect_fn=conectar)
    assert resposta["data"]["tenant"] == "acme"
    assert any("cardinalidade" in nota for nota in resposta["data"]["notes"])


async def test_erros_hoje_usa_a_janela_de_24h(cloud, conectar):
    resposta = await s.run_command("/erros hoje", cloud=cloud, connect_fn=conectar)
    assert resposta["data"]["window"] == "24h"
    assert resposta["data"]["errors"]["total"] == 3
    assert resposta["data"]["apps"] == []  # /erros não consulta o Prometheus


async def test_turso_fora_do_ar_nao_derruba_a_consulta(cloud):
    def explode():
        raise RuntimeError("banco indisponível")

    resposta = await s.run_command("/erros", cloud=cloud, connect_fn=explode)
    assert "indisponível" in resposta["data"]["errors"]["detail"]
    assert resposta["text"]  # a resposta chega mesmo assim


async def test_consulta_lenta_e_cancelada_no_deadline(conectar):
    """Critério de aceite: resposta em <10s. Estourar vira mensagem, não travamento."""
    import time

    class Lento(FakeEndpoint):
        def get(self, path, params):
            time.sleep(0.3)
            return super().get(path, params)

    resposta = await s.run_command(
        "/status", cloud=FakeCloud(Lento()), connect_fn=conectar, timeout=0.05
    )
    assert resposta["data"]["error"] == "timeout"
    assert "cancelada" in resposta["text"]


async def test_comando_desconhecido_devolve_ajuda():
    resposta = await s.run_command("/desligar tudo")
    assert resposta["command"] == s.CMD_AJUDA
    assert "/status" in resposta["text"]


async def test_acoes_lista_o_audit_log(cloud, conectar, banco):
    """`/acoes` (issue #19): o histórico de `action_audit` legível no celular."""
    import libsql as _libsql

    from ops_centro.turso import audit

    conn = _libsql.connect(database=banco)
    audit.record_action(
        conn,
        audit.ActionRecord(
            action=audit.ACTION_PAUSE_TOOL, target="search", status=audit.STATUS_OK,
            app_name="agents-platform", reason="7 falhas em 30min", ttl_seconds=900,
        ),
    )
    conn.close()

    resposta = await s.run_command("/acoes", cloud=cloud, connect_fn=conectar)
    assert resposta["data"]["actions"]["total"] == 1
    assert resposta["data"]["actions"]["actions"][0]["action"] == "pause_tool"
    # `_` é reservado no MarkdownV2 e sai escapado — o teste lê o texto como o Telegram lê.
    assert "pause\\_tool search" in resposta["text"] and "✅" in resposta["text"]
    assert "7 falhas em 30min" in resposta["text"].replace("\\", "")
    assert resposta["data"]["apps"] == []  # /acoes não consulta o Prometheus


# --- endpoint ----------------------------------------------------------------------------
@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_endpoint_exige_o_mesmo_token_do_webhook(client, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    assert client.post("/hermes/consulta", json={"command": "/status"}).status_code == 401
    resp = client.post(
        "/hermes/consulta", json={"command": "/ajuda"}, headers={"X-Alert-Token": "segredo"}
    )
    assert resp.status_code == 200
    assert resp.json()["command"] == s.CMD_AJUDA


def test_endpoint_falha_fechado_sem_token_configurado(client, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    assert client.post("/hermes/consulta", json={"command": "/status"}).status_code == 503
