# Sequência de Implementação — Issues em Aberto

> Gerado em 2026-08-05 após fechamento das issues #7–#10 e #12–#14 (já implementadas).
> Ordem definida por **valor pós-incidente + dependências técnicas**, não pelas milestones
> originais (que eram semanais do plano inicial).

## Mapa de dependências

```
            ┌─► #26 coletor de host ──► #28 alerta restart systemd
#25 rota ───┤        │
fora EC2    │        └─────────► #27 dead-man's switch (NoData)
            └─► #15 Telegram (só p/ alertas de app, não infra)
#4 bootstrap ──► #5 spans agent_api ──► #11 métricas prioritárias
                  │
                  └──► #6 gate Fase 1 (checklist formal)
                        │
#3 schema congelado ◄───┘ (validação em CI dos 3 repos)
#16/#17/#18/#19 (Hermes ações) ── dependem de #5+#11 (dados) e #15 (canal)
#20 ruleset main ── bloqueado por plano GitHub (reavaliar)
```

## Fase A — Resiliência do observador (urgente, pós-incidente 05/08)

### A1. #26 — Métricas de host da EC2 (memória, swap, disco, load) — ✅ implementada (05/08)
**Status:** Alloy v1.18.0 no compose de produção (`deploy/alloy/config.alloy`), collectors
cpu/filesystem/loadavg/meminfo/swap/systemd/time/uname, export via OTLP reusando o
endpoint do `.env`. **Pendências:** (a) confirmação visual das séries `node_*` no
Grafana Cloud (falta `GRAFANA_READ_TOKEN` na EC2 — criar service account `glsa_`
Viewer); (b) o SSM precisa receber o token novo (`writer-ops-write`) com espaço
literal + a var `OTEL_EXPORTER_OTLP_AUTH` — o `.env` da EC2 já está corrigido à mão,
mas `env-from-ssm.sh` sobrescreve na próxima execução.

- **O quê:** Alloy (Grafana Alloy) em container no compose de produção, com integração
  `prometheus.exporter.unix` (node_exporter embutido) + `prometheus.remote_write` para o
  Mimir do Grafana Cloud (OTLP já configurado no `.env` — mesmo endpoint).
- **Séries:** `node_memory_*`, `node_swap_*`, `node_filesystem_*`, `node_load1/5/15`,
  `node_systemd_unit_restarts_total` (habilita #28), `node_time_seconds` (habilita #27).
- **Labels (convenção):** `job="ec2-host"`, `environment`, `hostname`; sem `tenant_id`
  (métrica de infra).
- **Critério de aceite:** `make metrics-check` lista `node_*` no Mimir; dashboard
  `visao-geral.json` ganha row "Host".

### A2. #28 — Alerta de loop de restart de unit systemd
**Por quê agora:** teria disparado em 23/jun, 6 semanas antes do colapso.

- **O quê:** regra em `grafana/alerts/` (novo `host.yaml`):
  `rate(node_systemd_unit_restarts_total{job="ec2-host"}[15m]) > 0.02` (~2 restarts/15min)
  com `for: 10m`, severity crítica, rota `infra`.
- **Bônus da mesma regra:** memória/swap/disco acima de limiar (ex: `node_memory_Available_bytes
  / node_memory_MemTotal_bytes < 0.1`, `node_filesystem_avail / size < 0.1`).
- **Critério:** alerta dispara em teste de contato e a regra aparece em `make alerts-list`.

### A3. #27 — Dead-man's switch (NoData + heartbeat)
**Por quê agora:** hoje, se o host emudece, o Grafana silencia junto.

- **O quê:** série de heartbeat por fonte:
  - host: `up{job="ec2-host"}` (Alloy) — regra `NoData`/`missing series` com `for: 10m`;
  - receiver: `/healthz` já monitorado pelo `healthcheck.yml` — transformar em regra Mimir
    ou alerta externo;
  - alert rule Mimir: `vector(1)` sintético com `keep_firing_for` (recurso do Grafana
    Alerting para "ainda estou vivo").
- **Critério:** parar o container do receiver → alerta em ≤15min numa rota que NÃO passe
  pela EC2 (depende de A4).

### A4. #25 — Rota de notificação independente da EC2
**Por quê agora:** alerta de "EC2 caiu" não pode depender da EC2 para chegar.

- **O quê:** contact point **Telegram nativo do Grafana Cloud** (bot token + chat_id, sem
  passar pelo receiver) OU e-mail; política de roteamento: `severity=infra` →
  Telegram direto; demais → webhook `ops-centro-hermes` (receiver/Hermes).
- **Nota:** o token do bot não vive no `.env` da EC2 — vive no Grafana Cloud (RNF06).
- **Critério:** simular queda da EC2 (parar receiver) → alerta de infra chega no Telegram
  mesmo assim.

**Fim da Fase A:** qualquer recorrência do incidente de hoje gera Telegram em ≤15min sem
depender da máquina que caiu.

## Fase B — Fechar a instrumentação dos apps (Fase 1/2 do plano original)

### B1. #4 — blu_observability_bootstrap: resource_attributes + sampling (RNF02)
- `setup_observability(..., resource_attributes: dict | None, sampler=...)`.
- Padrão: `ParentBased(root=TraceIdRatioBased(0.1))`; **erros sempre amostrados** (100%
  em spans com status error — via `SpanProcessor` ou `FilteringSampler`); configurável por
  env (`OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` já respeitados pelo SDK).
- Convenções do ops-centro (`app_name`, `environment`, `tenant_id`, `version`) aplicadas
  no Resource; testes de unidade no `tests/` da lib.

### B2. #5 — Instrumentar agent_api: spans agent_execution + mcp_tool_call (RF03)
- Span pai `agent_execution` no ponto de execução de agente (run_routine / chat): status,
  duração, tokens, custo, modelo LLM.
- Span filho `mcp_tool_call` em cada chamada de tool MCP: tool_name, mcp_server, status,
  duração, retries, erro.
- Usar a lib do B1; nada de strings mágicas — nomes vêm do schema (B4).

### B3. #11 — Métricas prioritárias (§7)
- Com #5 emitindo, conferir `make metrics-check` (taxa de erro por agente/tool, latência
  p50/p95/p99, volume por tenant).
- Painéis dedicados no dashboard `agents-platform.json` (catálogo já existe em
  `ops_centro/metrics.py` — só "acender").

### B4. #6 — Gate da Fase 1 (checklist formal)
- Rodar `make validate` com os dois apps instrumentados; preencher o checklist §3 e o
  registro §6 de `docs/validacao-fase1.md` (PASS/FAIL por checagem + queries usadas).

### B5. #3 — Congelar o schema entre os três repos (RF02/RNF05)
- Fonte da verdade: `ops_centro/conventions.py`; réplica atual: `mcp_brain/common/
  telemetry_schema.py`; nova: schema consumido pela lib do B1.
- **Mecanismo:** teste em CI de cada repo que falha se o vocabulário divergir
  (cópia do JSON canônico + diff); script `make schema-sync` para propagar mudanças.
- Critério: mudança de nome de span exige PR nos 3 repos e CI pega divergência.

**Fim da Fase B:** dashboards de aplicação com dados reais, métricas §7 no Mimir e gate
da Fase 1 documentado.

## Fase C — Canal de aplicação + ações Hermes (Fases 3/4 do plano)

### C1. #15 — Notificação enriquecida no Telegram via Hermes (RF07 parte 2)
- Fechar o `TODO(#15)` em `ops_centro/receiver/app.py`: após enriquecer, repassar ao
  Hermes (endpoint/rota do Hermes na EC2) que formata e envia no Telegram.
- Com #25 pronto, esta rota cobre **alertas de aplicação** (não infra) — a circularidade
  já foi quebrada.

### C2. #16 — Consultas sob demanda (RF08)
- Skill do Hermes: "como estão os agentes hoje?" → PromQL no Mimir (erro/latência por
  app) + contagem de logs no Turso → resposta formatada no Telegram.

### C3. #17 — Ação autônoma: pausar tool MCP com falha recorrente (RF09)
- Skill do Hermes: consulta Mimir (`error rate por tool > limiar`) → chamada à API do
  mcp_brain para pausar a tool → registra no audit (#19).

### C4. #18 — Fluxo de confirmação para ações de maior impacto (RF10)
- Skill do Hermes com dois estágios: proposta → confirmação explícita no Telegram →
  execução; timeout cancela.

### C5. #19 — Audit log das ações do Hermes
- Tabela `actions` no Turso (timestamp, ação, alvo, autor da aprovação, resultado) +
  escrita em todo fluxo C2–C4; é a mitigação §10 do plano.

## Fase D — Hardening

### D1. #20 — Ruleset de proteção da main
- Bloqueado pelo plano do GitHub (ruleset não aplicável); manter
  `.github/rulesets/protect-main.json` versionado e **reavaliar no upgrade de plano**
  (marco: quando o repo ganhar 2+ contribuidores ou o plano mudar).

## Ordem sugerida de execução (resumo)

| Ordem | Issue | Entrega | Esforço |
|-------|-------|---------|---------|
| 1 | #26 | Alloy no compose + séries node_* no Mimir | M |
| 2 | #28 | regra restart systemd + memória/disco | S |
| 3 | #27 | dead-man's switch (NoData + keep_firing) | S |
| 4 | #25 | contact point Telegram nativo + roteamento por severity | S |
| 5 | #4 | bootstrap com resource attrs + sampling | M |
| 6 | #5 | spans agent_execution + mcp_tool_call no agent_api | G |
| 7 | #11 | conferir §7 + painéis | S |
| 8 | #6 | gate Fase 1 formalizado | S |
| 9 | #3 | validação de schema em CI (3 repos) | M |
| 10 | #15 | receiver → Hermes → Telegram | M |
| 11 | #16 | consultas sob demanda | M |
| 12 | #17 | pausar tool autônomo | M |
| 13 | #18 | fluxo de confirmação | M |
| 14 | #19 | audit log no Turso | S |
| 15 | #20 | reavaliar no upgrade de plano | — |

S = horas, M = meio dia, G = 1–2 dias. Fases A e B podem rodar em paralelo (repos
diferentes: ops-centro vs repo_platform/mcp_brain).
