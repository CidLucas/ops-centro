"""Testes do enriquecimento de alertas (RF07 — parte 1, issue #14).

O banco é um arquivo libsql local (sem rede, determinístico em CI), com as mesmas
migrations de produção — a query por `trace_id` só vale se for a mesma tabela e o mesmo
índice. As duas garantias que a issue pede:

1. alerta com `trace_id` conhecido chega com os logs daquele trace;
2. **falha do Turso não bloqueia a entrega** — timeout, exceção e banco ausente devolvem
   o alerta sem contexto, nunca um erro.
"""

import time

import libsql
import pytest
from fastapi.testclient import TestClient

from ops_centro.receiver import enrichment as e
from ops_centro.receiver.app import app
from ops_centro.turso import log_reader as lr
from ops_centro.turso.migrate import apply_migrations

pytestmark = pytest.mark.unit

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
STACK = "https://exemplo.grafana.net"


class Alerta:
    """Duck type do `GrafanaAlert` do receiver (o enriquecimento fala por protocolo)."""

    def __init__(self, labels=None, annotations=None, status="firing"):
        self.status = status
        self.labels = labels or {}
        self.annotations = annotations or {}


def alerta_padrao(**labels):
    base = {
        "alertname": "Agents Platform: taxa de erro de execução acima de 5%",
        "app_name": "agents-platform",
        "environment": "prod",
        "severity": "warning",
        **labels,
    }
    return Alerta(
        labels=base,
        annotations={
            "summary": "Agente pesquisa com 12% de execuções em erro",
            "description": "Execuções com status=error sobre o total.",
            "runbook_url": "https://github.com/CidLucas/ops-centro/blob/main/docs/alertas.md",
        },
    )


@pytest.fixture
def banco(tmp_path):
    """Banco local migrado, com logs de dois traces e dois níveis."""
    caminho = str(tmp_path / "logs.db")
    conn = libsql.connect(database=caminho)
    apply_migrations(conn)
    linhas = [
        ("2026-07-23T12:00:00.000+00:00", "agents-platform", "acme", TRACE, "ERROR",
         "tool search estourou o timeout", '{"tool": "search"}'),
        ("2026-07-23T12:00:01.000+00:00", "agents-platform", "acme", TRACE, "WARNING",
         "retry 1/3", None),
        ("2026-07-23T12:00:02.000+00:00", "agents-platform", "acme", "outro-trace", "ERROR",
         "outra execução, outro problema", None),
        ("2026-07-23T12:00:03.000+00:00", "agents-platform", "acme", None, "INFO",
         "execução concluída", None),
    ]
    conn.executemany(
        "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        linhas,
    )
    conn.commit()
    conn.close()
    return caminho


@pytest.fixture
def conectar(banco):
    return lambda: libsql.connect(database=banco)


# --- extração das labels ------------------------------------------------------------
def test_contexto_sai_das_labels_do_alerta():
    ctx = e.AlertContext.from_alert(alerta_padrao(tenant_id="acme", trace_id=TRACE))
    assert ctx.app_name == "agents-platform"
    assert ctx.tenant_id == "acme"
    assert ctx.trace_id == TRACE
    assert ctx.environment == "prod"
    assert ctx.severity == "warning"
    assert ctx.summary.startswith("Agente pesquisa")


def test_trace_id_tambem_e_aceito_nas_annotations():
    """Algumas integrações do Grafana copiam o exemplar para annotation, não para label."""
    alerta = Alerta(labels={"app_name": "agents-platform"}, annotations={"traceID": TRACE})
    assert e.AlertContext.from_alert(alerta).trace_id == TRACE


def test_contexto_de_alerta_sem_labels_nao_quebra():
    ctx = e.AlertContext.from_alert(Alerta())
    assert ctx.app_name is None and ctx.trace_id is None
    assert "alerta sem título" in ctx.headline()


def test_headline_diz_estado_severidade_e_alvo():
    ctx = e.AlertContext.from_alert(alerta_padrao(tenant_id="acme"))
    linha = ctx.headline()
    assert "[FIRING/warning]" in linha
    assert "agents-platform · acme · prod" in linha


# --- consulta ao Turso ----------------------------------------------------------------
def test_logs_por_trace(banco):
    conn = libsql.connect(database=banco)
    linhas = lr.logs_by_trace(conn, TRACE)
    assert [linha.level for linha in linhas] == ["WARNING", "ERROR"]  # mais recente primeiro
    assert all(linha.trace_id == TRACE for linha in linhas)


def test_logs_por_janela_ignora_info(banco):
    """INFO em janela larga afoga a linha que importa (e gasta row reads do free tier)."""
    conn = libsql.connect(database=banco)
    agora = lr.datetime(2026, 7, 23, 12, 5, tzinfo=lr.timezone.utc)
    linhas = lr.logs_by_window(conn, "agents-platform", minutes=30, now=agora)
    assert {linha.level for linha in linhas} == {"ERROR", "WARNING"}


def test_logs_por_janela_filtra_tenant(banco):
    conn = libsql.connect(database=banco)
    agora = lr.datetime(2026, 7, 23, 12, 5, tzinfo=lr.timezone.utc)
    assert lr.logs_by_window(conn, "agents-platform", tenant_id="outro", now=agora) == []
    assert lr.logs_by_window(conn, "agents-platform", tenant_id="acme", now=agora)


def test_janela_antiga_nao_traz_nada(banco):
    conn = libsql.connect(database=banco)
    agora = lr.datetime(2026, 7, 24, 12, 0, tzinfo=lr.timezone.utc)  # um dia depois
    assert lr.logs_by_window(conn, "agents-platform", minutes=30, now=agora) == []


def test_related_logs_cai_para_a_janela_quando_o_trace_nao_tem_log(banco):
    conn = libsql.connect(database=banco)
    agora = lr.datetime(2026, 7, 23, 12, 5, tzinfo=lr.timezone.utc)
    linhas, estrategia = lr.related_logs(
        conn, trace_id="trace-inexistente", app_name="agents-platform", now=agora
    )
    assert estrategia == "janela" and linhas


def test_related_logs_sem_trace_e_sem_app_nao_consulta(banco):
    conn = libsql.connect(database=banco)
    assert lr.related_logs(conn, trace_id=None, app_name=None) == ([], "nenhuma")


def test_metadata_vira_dicionario(banco):
    conn = libsql.connect(database=banco)
    erro = [linha for linha in lr.logs_by_trace(conn, TRACE) if linha.level == "ERROR"][0]
    assert erro.metadata_dict() == {"tool": "search"}


def test_mensagem_gigante_e_truncada(banco):
    conn = libsql.connect(database=banco)
    conn.execute(
        "INSERT INTO logs (timestamp, app_name, level, message) VALUES (?, ?, ?, ?)",
        ("2026-07-23T12:00:09.000+00:00", "agents-platform", "ERROR", "x" * 5_000),
    )
    conn.commit()
    linha = lr.logs_by_window(
        conn, "agents-platform", now=lr.datetime(2026, 7, 23, 12, 5, tzinfo=lr.timezone.utc)
    )[0]
    assert len(linha.message) == lr.MAX_MESSAGE_CHARS


# --- enriquecimento ---------------------------------------------------------------------
async def test_alerta_com_trace_conhecido_chega_com_os_logs(conectar):
    """Critério de aceite do #14."""
    (enriquecido,) = await e.enrich_alerts(
        [alerta_padrao(tenant_id="acme", trace_id=TRACE)], connect_fn=conectar
    )
    assert enriquecido.strategy == e.STRATEGY_TRACE
    assert enriquecido.enriched
    assert [linha.trace_id for linha in enriquecido.logs] == [TRACE, TRACE]
    assert "tool search estourou o timeout" in enriquecido.as_text()


async def test_alerta_sem_trace_usa_a_janela_do_app(conectar):
    agora = lr.datetime(2026, 7, 23, 12, 5, tzinfo=lr.timezone.utc)
    (enriquecido,) = await e.enrich_alerts(
        [alerta_padrao(tenant_id="acme")], connect_fn=conectar, now=agora
    )
    assert enriquecido.strategy == e.STRATEGY_WINDOW
    assert enriquecido.logs


async def test_payload_enriquecido_traz_resumo_logs_e_links(conectar, monkeypatch):
    monkeypatch.setenv("GRAFANA_STACK_URL", STACK)
    (enriquecido,) = await e.enrich_alerts(
        [alerta_padrao(tenant_id="acme", trace_id=TRACE)], connect_fn=conectar
    )
    corpo = enriquecido.as_dict()
    assert corpo["summary"].startswith("Agente pesquisa")
    assert corpo["tenant_id"] == "acme"
    assert len(corpo["logs"]) == 2
    assert corpo["links"]["trace"].startswith(f"{STACK}/explore?")
    assert TRACE in corpo["links"]["trace"]
    assert corpo["links"]["dashboard"].startswith(f"{STACK}/d/ops-centro-agents-platform")
    assert corpo["links"]["runbook"].endswith("docs/alertas.md")


async def test_teto_de_alertas_por_webhook(conectar):
    alertas = [alerta_padrao(trace_id=TRACE) for _ in range(10)]
    enriquecidos = await e.enrich_alerts(alertas, connect_fn=conectar, max_alerts=3)
    assert len(enriquecidos) == 3


async def test_webhook_sem_alertas_nao_consulta_nada():
    def explode():
        raise AssertionError("não deveria abrir conexão")

    assert await e.enrich_alerts([], connect_fn=explode) == []


# --- degradação (o alerta nunca é segurado) -----------------------------------------------
async def test_falha_do_turso_nao_bloqueia_o_alerta():
    """Critério de aceite do #14: sem banco, o alerta segue — só que pelado."""
    def explode():
        raise RuntimeError("connection refused")

    (enriquecido,) = await e.enrich_alerts([alerta_padrao(trace_id=TRACE)], connect_fn=explode)
    assert enriquecido.strategy == e.STRATEGY_UNAVAILABLE
    assert not enriquecido.enriched
    assert "connection refused" in enriquecido.reason
    assert "Sem logs correlacionados" in enriquecido.as_text()


async def test_timeout_do_turso_nao_bloqueia_o_alerta():
    class Lenta:
        def execute(self, *args, **kwargs):
            time.sleep(5)  # a thread fica para trás; o alerta não espera por ela

        def close(self):
            pass

    inicio = time.perf_counter()
    (enriquecido,) = await e.enrich_alerts(
        [alerta_padrao(trace_id=TRACE)], connect_fn=Lenta, timeout=0.05
    )
    assert time.perf_counter() - inicio < 2  # não esperou os 5s da consulta
    assert enriquecido.strategy == e.STRATEGY_UNAVAILABLE
    assert "timeout" in enriquecido.reason


async def test_sem_turso_configurado_o_enriquecimento_e_no_op(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    (enriquecido,) = await e.enrich_alerts([alerta_padrao(trace_id=TRACE)])
    assert enriquecido.strategy == e.STRATEGY_DISABLED
    assert not enriquecido.enriched


# --- links --------------------------------------------------------------------------------
def test_link_do_trace_aponta_para_o_tempo(monkeypatch):
    monkeypatch.setenv("GRAFANA_STACK_URL", STACK)
    url = e.trace_url(TRACE)
    assert url.startswith(f"{STACK}/explore?schemaVersion=1")
    assert "traceql" in url and TRACE in url


def test_link_do_dashboard_por_app(monkeypatch):
    monkeypatch.setenv("GRAFANA_STACK_URL", STACK)
    assert "ops-centro-file-memory" in e.dashboard_url("file-memory-mcp")
    assert "ops-centro-agents-platform" in e.dashboard_url("agents-platform", "prod")
    assert "var-environment=prod" in e.dashboard_url("agents-platform", "prod")
    # App desconhecido cai na visão geral em vez de gerar link quebrado.
    assert "ops-centro-visao-geral" in e.dashboard_url("app-que-nao-existe")


def test_sem_stack_url_nao_inventa_link(monkeypatch):
    monkeypatch.delenv("GRAFANA_STACK_URL", raising=False)
    assert e.trace_url(TRACE) == ""
    assert e.dashboard_url("agents-platform") == ""


def test_resumo_do_lote():
    resumo = e.summarize(
        [
            e.EnrichedAlert(e.AlertContext(), strategy=e.STRATEGY_TRACE),
            e.EnrichedAlert(e.AlertContext(), strategy=e.STRATEGY_DISABLED),
        ]
    )
    assert resumo["alerts"] == 2 and resumo["enriched"] == 0
    assert resumo["strategies"] == sorted([e.STRATEGY_TRACE, e.STRATEGY_DISABLED])


# --- ponta a ponta pelo endpoint -------------------------------------------------------------
PAYLOAD = {
    "status": "firing",
    "title": "Taxa de erro alta",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "erro",
                "app_name": "agents-platform",
                "tenant_id": "acme",
                "trace_id": TRACE,
                "severity": "warning",
            },
            "annotations": {"summary": "taxa de erro acima do limiar"},
        }
    ],
}


def test_webhook_devolve_o_alerta_enriquecido(banco, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    monkeypatch.setenv("TURSO_DATABASE_URL", banco)  # caminho de arquivo = banco local
    with TestClient(app) as client:
        resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "segredo"})
    assert resp.status_code == 202
    corpo = resp.json()
    assert corpo["enriched"] == 1 and corpo["log_lines"] == 2
    (detalhe,) = corpo["alerts_detail"]
    assert detalhe["strategy"] == "trace"
    mensagens = [linha["message"] for linha in detalhe["logs"]]
    assert mensagens == ["retry 1/3", "tool search estourou o timeout"]  # recentes primeiro


def test_webhook_aceita_o_token_como_bearer(banco, monkeypatch):
    """O contact point as-code manda o token nos dois formatos (roteamento.yaml)."""
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    monkeypatch.setenv("TURSO_DATABASE_URL", banco)
    with TestClient(app) as client:
        resp = client.post(
            "/alerts/grafana", json=PAYLOAD, headers={"Authorization": "Bearer segredo"}
        )
    assert resp.status_code == 202


def test_webhook_com_turso_quebrado_ainda_aceita(tmp_path, monkeypatch):
    """Banco sem a tabela `logs`: a consulta explode e o alerta passa mesmo assim."""
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "segredo")
    monkeypatch.setenv("TURSO_DATABASE_URL", str(tmp_path / "vazio.db"))
    with TestClient(app) as client:
        resp = client.post("/alerts/grafana", json=PAYLOAD, headers={"X-Alert-Token": "segredo"})
    assert resp.status_code == 202
    assert resp.json()["enriched"] == 0
    assert resp.json()["strategies"] == [e.STRATEGY_UNAVAILABLE]
