# Alertas as-code e enriquecimento (issues #12 e #14)

> Gerador: [`ops_centro/grafana/alerts.py`](../ops_centro/grafana/alerts.py) ·
> YAMLs: [`grafana/alerts/`](../grafana/alerts/) ·
> enriquecimento: [`ops_centro/receiver/enrichment.py`](../ops_centro/receiver/enrichment.py).

Este é o runbook apontado por `runbook_url` em toda regra: se você chegou aqui por um
alerta, comece pela §5.

O caminho completo de um alerta:

```
métrica no Mimir ──limiar──▶ alert rule (§1) ──notification policy (§3)──▶ contact point webhook
                                                                                  │
                                          receiver /alerts/grafana ◀──────────────┘
                                                   │
                              enriquece no Turso por trace_id (§4) ──▶ Hermes → Telegram (#15)
```

## 1. As regras

Geradas a partir do catálogo de métricas da §7 — regra que cita métrica fora do catálogo
não passa no teste, e regra sem `app_name` nas labels também não (o enriquecimento
precisaria adivinhar de quem é o alerta).

| Grupo | Arquivo | Regras |
| --- | --- | --- |
| `ops-centro-apps` | [`apps.yaml`](../grafana/alerts/apps.yaml) | erro por agente (5%), erro por tool (10%), p95 de execução (30s), p95 de LLM (60s), falha de ingestão (10%), erro de tool do File Memory (10%), p95 de query de memória (5s), erro por tenant (20%, crítico) |
| `ops-centro-free-tier` | [`free-tier.yaml`](../grafana/alerts/free-tier.yaml) | logs e traces a 70%/90% da cota; séries ativas em 7.000/9.500 |
| `ops-centro-turso` | [`turso-retencao.yaml`](../grafana/alerts/turso-retencao.yaml) | banco de logs a 70%/90% do teto; job de retenção parado há 36h |

`make alerts-list` imprime a tabela com limiar e `for` de cada uma.

**Os limiares são o ponto de partida da Fase 3**, não verdade revelada: os apps ainda não
emitem volume suficiente para calibração honesta (#5/#7). A regra prática para recalibrar,
depois de duas semanas de tráfego: p95 de alerta = p95 observado + 50%; taxa de erro =
média em regime + 3 pontos. Mudar um número é mudar `ops_centro/grafana/alerts.py` e rodar
`make alerts`.

**Por que cada regra tem `for`:** alerta sem histerese dispara em pico instantâneo, e um
canal que grita por pico vira canal silenciado. Os `for` variam de 10m (erro) a 1h (cota
mensal do free tier) conforme a velocidade do que está sendo medido.

## 2. Fluxo de trabalho

```bash
make alerts         # (re)gera os YAMLs a partir do gerador
make alerts-check   # falha se o YAML commitado divergir (mesmo gate do teste)
make alerts-list    # regras e limiares
make alerts-apply   # publica no Grafana Cloud
```

Publicar é idempotente e **convergente**: cada grupo vai inteiro por
`PUT /api/v1/provisioning/folder/{uid}/rule-groups/{grupo}`, então regra removida do repo
também some do stack — coisa que um POST regra a regra não faz.

Credenciais (ver [secrets.md](secrets.md)):

| Variável | Para quê |
| --- | --- |
| `GRAFANA_STACK_URL` + `GRAFANA_API_TOKEN` | publicar (precisa de `alert.rules:write` e `alert.notifications:write` — role *Editor*) |
| `RECEIVER_WEBHOOK_URL` | URL pública do receiver, para o contact point |
| `ALERT_WEBHOOK_TOKEN` | o mesmo token que o receiver valida |
| `GRAFANA_PROM_DS_UID` / `GRAFANA_USAGE_DS_UID` | uids dos datasources (defaults: `grafanacloud-prom` / `grafanacloud-usage`) |

Os arquivos em `grafana/alerts/` **não carregam segredo**: só `${ALERT_WEBHOOK_TOKEN}` e
`${RECEIVER_WEBHOOK_URL}`, resolvidos do ambiente na hora do `--apply` (RNF06). É por isso
que eles podem ser commitados e ainda assim usados como provisionamento por arquivo.

> `--apply` substitui a **árvore de roteamento do org** (o Grafana só tem uma). Num stack
> com roteamento pré-existente, use `--skip-policy` e faça a mudança de roteamento na mão.

> **Estado (2026-07-23):** as 17 expressões foram rodadas contra o stack
> `radiantfennec1578` como query instantânea — todas válidas, e as do free tier já
> devolvem série (as métricas de uso existem com os nomes usados aqui). A **publicação**
> ainda não foi feita: o contact point precisa da URL pública do receiver, que depende do
> deploy na EC2 ([#13](deploy.md)). Ordem certa: deploy → `RECEIVER_WEBHOOK_URL` no `.env`
> → `make alerts-apply` → *Test* no contact point.

## 3. Roteamento

[`roteamento.yaml`](../grafana/alerts/roteamento.yaml): contact point `ops-centro-hermes`
(webhook → `POST /alerts/grafana`) e a policy que agrupa por `alertname` + `app_name` +
`tenant_id`.

Agrupar não é detalhe de configuração: um incidente que atinge 40 tenants sem agrupamento
vira 40 mensagens no Telegram — a tempestade que faz as pessoas silenciarem o canal. Os
tempos: `group_wait` 30s (10s para crítico), `group_interval` 5m, `repeat_interval` 4h (1h
para crítico).

**Autenticação:** o contact point manda o token nos dois formatos que o receiver aceita —
header `X-Alert-Token` (o do contrato) e `Authorization: Bearer` (suportado por qualquer
versão do webhook do Grafana, inclusive as anteriores a headers customizados). O receiver
compara com `secrets.compare_digest` e responde 401 sem ele, 503 se ele não estiver nem
configurado (falha fechada).

## 4. Enriquecimento (issue #14)

O receiver não repassa o alerta cru. Para cada alerta (até 5 por webhook) ele:

1. extrai `app_name`, `tenant_id`, `trace_id`, `environment` e `severity` das labels — e o
   `trace_id` também das annotations, porque algumas integrações o colocam lá;
2. consulta o Turso: os logs daquele `trace_id` ou, na falta dele, os `ERROR`/`WARNING` da
   janela recente do app/tenant (`ops_centro/turso/log_reader.py`);
3. monta o payload com resumo, logs, link do trace no Tempo e link do dashboard do app já
   filtrado pelo ambiente.

O corpo da resposta do webhook é esse payload — é o que o envio ao Telegram (#15) consome.

**O enriquecimento nunca segura o alerta.** A consulta roda com deadline
(`ALERT_ENRICHMENT_TIMEOUT`, default 2s) numa thread; timeout, Turso fora do ar ou
`TURSO_DATABASE_URL` ausente devolvem o alerta sem logs, com `strategy` dizendo o motivo
(`indisponível` / `desativado`). Alerta pobre chega; alerta atrasado não é alerta.

O próprio enriquecimento é medido: `ops_centro_alert_enrichment_total{status}` e
`ops_centro_alert_enrichment_duration_seconds` (catálogo em `ops_centro/metrics.py`). Uma
subida de `status="error"` é o aviso de que os alertas estão chegando pelados.

## 5. Se você chegou aqui por causa de um alerta

1. **Leia a `description`** — cada regra traz a ação associada; alerta sem ação é ruído, e
   a revisão de PR cobra isso.
2. **Olhe os logs que vieram junto.** Vieram de um `trace`? É a execução exata que falhou.
   Vieram da `janela`? É uma amostra do que estava errado por volta do horário — não
   conclua causalidade a partir dela.
3. **Abra o link do trace** (Tempo) ou do dashboard, ambos já no recorte certo.
4. **Alerta sem contexto** (`strategy: indisponível`): o problema pode ser o Turso, não o
   app. Confira `ops_centro_alert_enrichment_total{status="error"}` antes de investigar o
   alerta em si.

## 6. Teste de ponta a ponta

Sem esperar um incidente:

```bash
# 1. o receiver aceita? (local ou na EC2)
curl -fsS -X POST http://localhost:8080/alerts/grafana \
  -H "X-Alert-Token: $ALERT_WEBHOOK_TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"firing","alerts":[{"status":"firing",
       "labels":{"alertname":"teste","app_name":"agents-platform","tenant_id":"acme",
                 "trace_id":"<um trace_id que exista no Turso>","severity":"warning"},
       "annotations":{"summary":"teste de ponta a ponta"}}]}' | jq

# 2. o Grafana chega até ele? Alerting → Contact points → ops-centro-hermes → Test
```

O critério de aceite do #12 é o segundo: o teste do contact point sai do Grafana Cloud e
volta como `202` no log do receiver. O do #14 é o primeiro trazer os logs do trace no
corpo da resposta.
