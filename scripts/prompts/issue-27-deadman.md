# Issue #27 — Dead-man's switch: heartbeat do receiver + regra NoData roteada fora da EC2

## Contexto

Repo: `/tmp/ops-centro` (CidLucas/ops-centro), main em `4e8b058` (working tree limpo).

**Problema (incidente de 05/08/2026):** a EC2 do Hermes não errou — **emudeceu**. O
`sshd` aceitava TCP mas nunca respondia; o Tailscale dizia `Online=True` mas não
completava handshake. Todas as regras atuais disparam sobre **limiar de métrica**
(taxa de erro, latência) e pressupõem que a série continua chegando. Nenhuma dispara
quando a série **para de existir**. O problema só foi notado quando um humano tentou
usar a máquina, horas depois.

**O que já existe (fundação, NÃO refazer):**
1. **Rota fora da EC2** (issue #25, já publicada): contact point Telegram nativo do
   Grafana Cloud (`TELEGRAM_NAME = "ops-centro-telegram"`) + rota
   `component="ec2-host"` → vai DIRETO ao Telegram, sem depender da EC2. É a rota que
   um dead-man precisa: se a EC2 cai, o Grafana Cloud (gerenciado) continua entregando.
2. **Padrão NoData** no gerador as-code (#12): o dataclass `Rule` já tem
   `no_data: str = "NoData"` e há dois exemplos de `no_data="Alerting"` (a ausência de
   sinal É o sintoma): `ops-centro-retencao-parada` (build_turso) e
   `ops-centro-host-coletor-parado` (build_host, vigia `up{job="integrations/unix"}`).
3. **OTLP do receiver funcionando**: o app usa `setup_observability` (blu
   observability bootstrap) com `OTEL_EXPORTER_OTLP_ENDPOINT`/`_HEADERS` do `.env` e
   emite métricas `ops_centro_*` direto ao Grafana Cloud (o Alloy do host é outra
   perna, `job="integrations/unix"`).

**O que falta (o coração do #27):** o receiver NÃO emite série contínua — o counter
`ops_centro_alerts_received_total` só aparece quando há alerta (verificado em 05/08:
zero métricas `ops_centro_*` no Mimir em regime). Sem série contínua não há NoData
para observar. Falta:
1. **Heartbeat**: o receiver emite um counter `ops_centro_heartbeat_total` em
   intervalo fixo (a cada 30s, via task asyncio no lifespan) → série sempre presente
   no Mimir;
2. **Regra NoData**: alert rule que dispara quando essa série some (> 10 min) ou
   congela (increase = 0), roteada pela rota do #25 (`component="ec2-host"`) — fora
   da EC2.

## Solução

```
receiver (EC2) ──OTLP──▶ Grafana Cloud (Mimir)
   └─ task asyncio: ops_centro_heartbeat_total += 1 a cada 30s
regra: sum(increase(ops_centro_heartbeat_total[10m])) < 1  OU  NoData → Alerting
       → component="ec2-host" → rota #25 → Telegram nativo (não depende da EC2)
```

Dois modos de falha cobertos pela MESMA regra:
- **Série some** (EC2 caiu, receiver morreu, OTLP quebrou): `NoData → Alerting`;
- **Série congela** (receiver vivo mas sem exportar, ex: OTLP travado):
  `increase[10m] == 0 < 1` → dispara igual.

## Tarefa — TDD vertical (1 teste RED → 1 GREEN por ciclo)

Edite **apenas**: `ops_centro/receiver/heartbeat.py` (novo),
`ops_centro/receiver/app.py`, `ops_centro/metrics.py`, `ops_centro/grafana/alerts.py`,
`tests/test_heartbeat.py` (novo), `tests/test_metrics.py`, `tests/test_alerts.py` e
`docs/alertas.md`. NÃO rode deploy/`--apply` (publicação é passo posterior com revisão
humana). NÃO toque no roteamento (`build_policy`/`build_routing`) — a rota do #25 já
existe e a regra nova reusa `component="ec2-host"`.

### 1. `ops_centro/receiver/heartbeat.py` (novo) — o heartbeat

Módulo pequeno, no padrão do repo (lazy meter, nunca levanta, testável):

- `DEFAULT_INTERVAL_SECONDS = 30`.
- `start(interval: int = DEFAULT_INTERVAL_SECONDS, *, sleep=asyncio.sleep) -> asyncio.Task`:
  cria o counter OTel `ops_centro_heartbeat_total` (meter `ops_centro.receiver.heartbeat`,
  descrição curta citando o #27) e devolve uma task que incrementa
  `counter.add(1, common_labels(APP_OPS_CENTRO))` a cada `interval` segundos. O counter
  é criado dentro de try/except (telemetria nunca derruba o receiver — mesmo padrão de
  `_record_alert` no app.py).
- `stop(task: asyncio.Task | None) -> None`: cancela a task se viva, sem levantar
  (CancelledError silencioso).
- Uma função `_interval_seconds() -> int` lendo env `HEARTBEAT_INTERVAL_SECONDS`
  (default 30) — para o deploy poder ajustar sem rebuild.
- **Nunca levanta**: se o SDK OTel não estiver pronto, o incremento é no-op (mesma
  filosofia do resto: sem OTLP configurado vira no-op silencioso).

### 2. `ops_centro/receiver/app.py` — ligar no lifespan

- No `lifespan()` (start): `_heartbeat_task = heartbeat.start()` e no shutdown:
  `heartbeat.stop(_heartbeat_task)` (antes do `shutdown_observability`, para o último
  batimento ser exportado).
- Variável de módulo `_heartbeat_task: asyncio.Task | None = None` (padrão do app).

### 3. `ops_centro/metrics.py` — catálogo

Adicione, na seção `ops-centro` do `CATALOG` (junto das outras `ITEM_SELF`,
`status=EMITTING`):

```python
Metric(
    name="ops_centro_heartbeat_total",
    app=APP_OPS_CENTRO,
    kind=COUNTER,
    unit="1",
    labels=(),
    item=ITEM_SELF,
    description="Batimento do receiver a cada 30s — a série contínua que torna o "
    "dead-man's switch possível (issue #27): NoData ou increase=0 por >10m = EC2 "
    "emudeceu.",
    status=EMITTING,
),
```

### 4. `ops_centro/grafana/alerts.py` — regra NoData

Adicione UMA regra nova ao fim do `build_host()` (grupo `ops-centro-host`,
`interval_seconds=60`):

```python
Rule(
    uid="ops-centro-host-deadman",
    title="Host: heartbeat do receiver ausente há 10m (dead-man's switch)",
    expr="sum(increase(ops_centro_heartbeat_total[10m])) or vector(0)",
    op="lt",
    threshold=1,
    duration="10m",
    severity=SEVERITY_CRITICAL,
    component="ec2-host",  # rota do #25: Telegram nativo, NÃO depende da EC2
    labels={ATTR_APP_NAME: "ops-centro"},
    lookback=3600,
    no_data="Alerting",  # ausência de sinal É o sintoma (padrão do #27)
    summary="O receiver do ops-centro não emite heartbeat há 10m",
    description=(
        "Nenhum incremento de ops_centro_heartbeat_total nos últimos 10m — a EC2 "
        "emudeceu (como em 05/08/2026), o container do receiver morreu, ou o OTLP "
        "quebrou. O alerta sai pela rota do #25 (Telegram nativo do Grafana Cloud), "
        "que não depende da máquina que parou de falar.\n"
        "Ações: (1) conferir se a EC2 responde (ssh/tailscale); (2) "
        "`docker ps` no receiver; (3) `docker logs ops-centro-receiver` e o healthz."
    ),
),
```

Atenção ao `expr`: `sum(increase(ops_centro_heartbeat_total[10m])) or vector(0)` —
quando a série existe mas congela, vira `0` e `lt 1` dispara; quando a série SOME, a
query retorna NoData e o `no_data="Alerting"` dispara. (O `or vector(0)` garante que
"congelou" também alerte — o `sum(...)` sozinho devolveria NoData nos dois casos.)
Atualize a docstring do `build_host()` (ela menciona "o #27 completo é outra issue" —
agora é esta) e o `header` do grupo se citar o mini dead-man.

### 5. Testes (RED primeiro)

`tests/test_heartbeat.py` (novo):
- `test_heartbeat_incrementa_o_counter_em_intervalos(mocker)`: mocka o meter OTel e
  `asyncio.sleep` (ou usa um intervalo minúsculo com `sleep` real curto); roda a task,
  espera ~2 intervalos, confirma `counter.add` chamado ≥ 2x com
  `{**common_labels(APP_OPS_CENTRO)}`.
- `test_heartbeat_stop_cancela_a_task`: `stop()` numa task viva → task cancelada, sem
  exceção.
- `test_heartbeat_sem_otel_nao_levanta(mocker)`: mocka `opentelemetry.metrics` para
  levantar; `start()` não propaga exceção e a task morre silenciosa (ou incrementa
  no-op).
- `test_intervalo_vem_do_env(monkeypatch)`: `HEARTBEAT_INTERVAL_SECONDS=5` →
  `_interval_seconds() == 5`; sem env → 30.

`tests/test_metrics.py`:
- Ajuste QUALQUER invariante de contagem que o catálogo novo quebre (procure por
  `len(` no arquivo e atualize com justificativa) e adicione:
  `ops_centro_heartbeat_total` existe no catálogo, é `ITEM_SELF` e `EMITTING`.

`tests/test_alerts.py`:
- `test_grupo_host_tem_quatro_regras...` → agora 5 regras (atualize o nome e o número).
- Nova: `test_deadman_alerta_na_ausencia_de_sinal()` — a regra
  `ops-centro-host-deadman` tem `no_data == "Alerting"`, `component == "ec2-host"`,
  `duration == "10m"` e a expr cobre os dois modos (`or vector(0)` presente).
- Confirme que a rota do #25 já casa: `build_policy()` tem rota com
  `object_matchers == [["component", "=", "ec2-host"]]` (não altere o roteamento).

### 6. `docs/alertas.md`

Adicione uma seção curta "Dead-man's switch (issue #27)": o que é, por que NoData, o
caminho da rota (#25, Telegram nativo), os dois modos de falha cobertos, e o teste de
mesa (parar o receiver e confirmar alerta em < 15 min — será feito no deploy).

## Critérios de aceite

1. `uv run python -m pytest tests/test_heartbeat.py tests/test_metrics.py tests/test_alerts.py -q` — verde.
2. `uv run python -m pytest tests/ -q` — tudo verde (252 + novos).
3. `uv run ruff check ops_centro tests` — limpo.
4. `uv run python -m ops_centro.grafana.alerts --check` — roteamento e grupos em dia
   (se o comando existir; senão o teste de arquivos cobre).
5. O receiver emite `ops_centro_heartbeat_total` a cada 30s (env-configurável) e a
   regra dispara com `NoData → Alerting` OU `increase < 1`, roteada para o Telegram
   nativo (component ec2-host).
6. NÃO rodar deploy/--apply, NÃO tocar em `build_policy`/`build_routing`, NÃO tocar
   em outros módulos do receiver.
