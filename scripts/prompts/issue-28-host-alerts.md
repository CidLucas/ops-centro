# Issue #28 — Alertas de host: loop de restart systemd + memória/disco (pega a causa, não o sintoma)

## Contexto

Repo: `/tmp/ops-centro` (CidLucas/ops-centro). Estamos na Fase A da sequência de
implementação. O incidente de 05/08/2026: a unit `hermes-dashboard.service` ficou em
loop de restart (800–1300 restarts/hora) por ~6 semanas (desde 23/jun) sem nenhum
alerta — a máquina exauriu memória e swap e ficou inacessível. Só um humano descobriu.

As métricas de host JÁ estão chegando ao Mimir via Alloy (issue #26, implementada e
verificada) com **`job="integrations/unix"`** (job vem dos targets do exporter.unix —
padrão da integração Unix do Grafana Cloud).

**Importante (descoberto na #26):** o node_exporter embutido no Alloy v1.18 **NÃO
expõe `node_systemd_unit_restarts_total`** (só 8 métricas systemd). A detecção de loop
de restart usa **`changes(node_systemd_unit_state{state="failed"}[15m])`**: num loop com
`Restart=always`, a unit alterna failed↔activating; `changes()` conta as transições de
estado (robusto mesmo com scrape de 30s). Verificado no Mimir: 202 séries (uma por
unit), todas = 0 num sistema estável.

## Tarefa

Edite **apenas**:
1. `ops_centro/grafana/alerts.py` — adicione `build_host()` (novo `RuleGroup`, arquivo
   `host.yaml`, grupo `ops-centro-host`, `interval_seconds=60`) e inclua-o em
   `GROUP_BUILDERS` (depois de `build_turso`). Siga exatamente o padrão de
   `build_turso()` (docstring com o porquê, `Rule(...)` com todos os campos).
2. `tests/test_alerts.py` — atualize o set esperado em `test_todos_os_arquivos_existem_no_repo`
   para incluir `"host.yaml"` e adicione testes do grupo host (veja abaixo).
3. Gere o YAML: `uv run python -m ops_centro.grafana.alerts --write` (commite
   `grafana/alerts/host.yaml` gerado). **NÃO rode `--apply`** — a publicação no Grafana
   Cloud é feita depois, por revisão humana.

### As 4 regras (queries VALIDADAS no Mimir em 05/08/2026)

Todas com `component="ec2-host"` e `labels={ATTR_APP_NAME: "ops-centro-host"}`
(a validação `validate_rules` exige `app_name`; o enriquecimento do receiver por
trace_id não se aplica a host, mas a label é o contrato do repo — "ou a query agrupa,
ou a regra fixa").

1. **`ops-centro-host-restart-loop`** — o alerta principal (pega a causa do incidente)
   - expr: `changes(node_systemd_unit_state{job="integrations/unix", state="failed"}[15m])`
   - op: `gt`, threshold: `6` (≥3 restarts em 15min ≈ 12/h — o incidente foi 800–1300/h)
   - duration: `5m`, severity: `critical`
   - summary: `Unit {{ $labels.name }} em loop de restart ({{ printf "%.0f" $values.A.Value }} transições failed/15m)`
   - description: conte a história — unit com `Restart=always` + `StartLimitIntervalSec=0`
     (freio desligado) rodando 800–1300 restarts/h por semanas até exaurir memória/swap;
     como investigar: `systemctl status <name>`, `journalctl -u <name> --since -1h`,
     conferir `StartLimitBurst`/`StartLimitIntervalSec` na unit; se o processo sobe e
     morre por porta ocupada, `ss -tlnp` na porta.
2. **`ops-centro-host-memoria`** — memória disponível < 10%
   - expr: `100 * node_memory_MemAvailable_bytes{job="integrations/unix"} / clamp_min(node_memory_MemTotal_bytes{job="integrations/unix"}, 1)`
   - op: `lt`, threshold: `10`, duration: `10m`, severity: `warning`
   - summary: `Memória disponível em {{ printf "%.0f" $values.A.Value }}% do total`
   - description: piso `clamp_min` evita NaN; agora mede ~45% (regime saudável); < 10%
     é thrashing iminente (o que aconteceu no incidente).
3. **`ops-centro-host-disco`** — raiz < 10% livre
   - expr: `100 * node_filesystem_avail_bytes{job="integrations/unix", mountpoint="/"} / clamp_min(node_filesystem_size_bytes{job="integrations/unix", mountpoint="/"}, 1)`
   - op: `lt`, threshold: `10`, duration: `10m`, severity: `warning`
   - summary: `Disco raiz com {{ printf "%.0f" $values.A.Value }}% livre`
   - description: agora mede ~18% — o disco cheio é o que mata o gateway do Hermes
     (Errno 28) e orfana runs; limpar `~/.hermes/logs`, `~/.cache`, `opencode.db`.
4. **`ops-centro-host-coletor-parado`** — o Alloy/host sumiu (mini dead-man's switch;
   o #27 completo — NoData + keep_firing + rota independente — é outra issue)
   - expr: `up{job="integrations/unix"}`
   - op: `lt`, threshold: `1` (up=1 é saudável; some → 0 ou NoData), duration: `10m`,
     severity: `critical`, no_data: `"Alerting"` (ausência de sinal É o sintoma)
   - summary: `Coletor de métricas de host sem sinal há 10m`
   - description: o Alloy da EC2 parou de reportar — host caiu, container morreu ou o
     OTLP falhou; é o alerta que o incidente de 05/08 não tinha.

### Testes a adicionar (padrão do repo, `pytestmark = pytest.mark.unit`)

- `test_grupo_host_tem_quatro_regras_com_job_de_host`: todas as exprs de `build_host().rules`
  contêm `job="integrations/unix"`.
- `test_restart_loop_usa_changes_no_estado_failed`: a regra `ops-centro-host-restart-loop`
  tem `changes(` e `state="failed"` na expr e `severity == "critical"`.
- `test_coletor_parado_alerta_na_ausencia_de_sinal`: a regra `ops-centro-host-coletor-parado`
  tem `no_data == "Alerting"`.

## Critérios de aceite

1. `uv run python -m ops_centro.grafana.alerts --check` passa (YAML em dia com o gerador).
2. `uv run python -m pytest tests/ -q` — tudo verde (lint: `uv run ruff check ops_centro tests`).
3. `grafana/alerts/host.yaml` existe com as 4 regras (grupo `ops-centro-host`).
4. **Nenhuma** mudança em `grafana/alerts/roteamento.yaml`, `apps.yaml`,
   `free-tier.yaml`, `turso-retencao.yaml`, `ops_centro/metrics.py`,
   `ops_centro/conventions.py`, `Makefile` ou no compose.
5. NÃO rodar `--apply` (nada é publicado no Grafana Cloud nesta tarefa).
