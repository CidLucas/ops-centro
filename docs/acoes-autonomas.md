# Ação autônoma: pausar tool MCP com falha recorrente (RF09, issue #17)

> Código: [`ops_centro/receiver/actions.py`](../ops_centro/receiver/actions.py) ·
> auditoria: [`ops_centro/turso/audit.py`](../ops_centro/turso/audit.py) +
> migration [`0004`](../db/migrations/0004_actions_audit.sql).

Primeira ação automatizada da §8 do plano e o último critério de sucesso da §12: uma ação
de baixo risco funcionando de ponta a ponta, sem humano no meio.

```
alerta de erro por tool (#12) ─▶ receiver enriquece (#14)
        │
        ├─ 1. decide     N falhas da MESMA tool em X min, confirmadas no Turso
        ├─ 2. executa    POST no admin_api do app → tool pausada por TTL
        ├─ 3. audita     linha em `action_audit` com gatilho, trace_id e TTL
        ├─ 4. avisa      mensagem no Telegram pelo canal do #15, dizendo o quê e por quê
        └─ 5. despausa   no vencimento do TTL — e avisa de novo
```

## 1. A regra de decisão

| Condição | Default | Env var |
| --- | --- | --- |
| Alerta `firing` com label `tool` (ou `tool_name`) | — | — |
| Falhas da mesma tool na janela, contadas no Turso | ≥ 5 | `AUTONOMOUS_PAUSE_THRESHOLD` |
| Janela da contagem | 30 min | `AUTONOMOUS_PAUSE_WINDOW_MINUTES` |
| TTL da pausa | 900s (15 min) | `AUTONOMOUS_PAUSE_TTL_SECONDS` |

A contagem sai de `logs` no Turso, pelo `tool` no `metadata` JSON que o app grava junto do
log (RF05). **O alerta sozinho não basta:** ele é o sintoma agregado; a contagem é a prova
de que a mesma tool falhou repetidamente. Sem Turso, sem contagem — e sem pausa.

Uma tool por webhook, no máximo. Se o incidente derrubou três tools ao mesmo tempo, o
problema quase certamente não é tool nenhuma, e pausar três é agravar o incidente.

## 2. As três travas

**Falha fechada, ao contrário do enriquecimento.** O #14 entrega o alerta mesmo sem
contexto, de propósito: alerta pobre ainda é alerta. Aqui é o inverso, porque as
consequências são inversas — alerta pobre incomoda, pausa indevida tira uma tool do ar.
Qualquer dúvida (sem label, sem Turso, contagem abaixo do limiar, alerta já resolvido)
resulta em **nenhuma ação**, com o motivo registrado no log.

1. **Kill switch** — `AUTONOMOUS_ACTIONS=off` (ou `0`/`false`/`no`) desliga toda ação
   autônoma sem deploy. É a alavanca que se puxa às 3h da manhã quando o automatismo está
   atrapalhando. Default: ligado — quem não configurou endpoint admin já não age.
2. **Cooldown** — tool com pausa vigente não é pausada de novo. Sem isso, cada avaliação
   da regra reiniciaria o TTL e a "pausa temporária" viraria permanente.
3. **Auditoria sempre** — inclusive quando a ação é bloqueada. Ação sem trilha é
   exatamente o que a mitigação §10 do plano existe para impedir.

## 3. Contrato com o `admin_api` dos apps (cross-repo)

O endpoint mora no repo do app — no mcp_brain, a `admin_api` é o lugar natural. O receiver
chama, autenticado com `ADMIN_API_TOKEN` (`Authorization: Bearer` + `X-Admin-Token`):

```http
POST <ADMIN_API_..._URL>/admin/tools/pause
{"tool_name": "search", "ttl_seconds": 900, "reason": "7 falhas em 30min (limiar 5)"}

POST <ADMIN_API_..._URL>/admin/tools/resume
{"tool_name": "search"}
```

Qualquer 2xx é sucesso. O que o app deve garantir: `pause` é idempotente, e o `ttl_seconds`
faz a tool voltar sozinha mesmo que o ops-centro desapareça.

| App | Variável |
| --- | --- |
| agents-platform | `ADMIN_API_AGENTS_PLATFORM_URL` |
| file-memory-mcp | `ADMIN_API_FILE_MEMORY_URL` |

Sem URL configurada para o app do alerta, a ação vira `bloqueado` no audit e **nada é
chamado** — o estado de hoje, enquanto os endpoints não existem do lado de lá.

## 4. TTL: dobrado de propósito

O TTL vai nos **dois** lados:

- no corpo da chamada, para o app despausar sozinho mesmo que o ops-centro suma;
- em `expires_at` na tabela `action_audit`, de onde `sweep_expired()` despausa e avisa,
  mesmo que o app não tenha implementado expiração.

O estado mora no banco, não em memória: um restart do receiver no meio da janela não deixa
tool pausada para sempre. O sweep roda no start do serviço e depois de cada webhook — o
batimento mais frequente que este serviço tem — e à mão com `make actions-sweep`.

A linha de `resume_tool` no audit é o que cancela a pausa. Por isso o sweep é idempotente:
uma segunda passada não encontra nada, e uma tool despausada à mão não é despausada de
novo.

## 5. O que chega no Telegram

Transparência é critério de aceite: toda ação vira mensagem pelo canal do #15
([hermes.md](hermes.md)), com `kind: "action"`.

```
🤖 Tool search pausada por 15 min
agents-platform · acme · ação autônoma

Motivo: 7 falhas em 30min (limiar 5)
Evidência: 7 falhas no Turso.
Gatilho: Agents Platform: taxa de erro por tool MCP acima de 10% · 7 falha(s)/30min
Despausa automática em 2026-07-24T07:06:10+00:00.
Desligue com AUTONOMOUS_ACTIONS=off; reverta agora com /despausar search (pede confirmação).
```

No vencimento: `▶️ Tool despausada: search`. Falha ao despausar vira `⚠️` — pausa que
deveria ter terminado e não terminou é informação operacional, não silêncio.

## 6. Auditoria de todas as ações (issue #19)

Uma tabela para as duas famílias de ação: a autônoma daqui e a
[confirmada do #18](acoes-confirmadas.md). Mitigação §10 do plano — "logar todas as ações
tomadas" — e a resposta de investigação quando alguma delas der errado.

Tabela `action_audit`: `created_at`, `actor`, `action`, `target`, `app_name`, `tenant_id`,
`trace_id`, `triggered_by`, `reason`, `ttl_seconds`, `expires_at`, `status`, `detail`.

| Coluna | Por que ela existe |
| --- | --- |
| `actor` | `ops-centro` (autônoma) ou `telegram:<user>` (confirmada) — a diferença entre "o sistema decidiu" e "alguém decidiu" |
| `triggered_by` | o gatilho: alertname + contagem de falhas, ou o chat que pediu |
| `trace_id` | liga a ação ao alerta que a motivou e aos logs daquela execução (RF05) |
| `expires_at` | quando a pausa vence — é por ela que o despausar sobrevive a um restart |
| `status` | `ok` \| `error` \| `bloqueado` \| `proposto` \| `cancelado` |

`bloqueado`, `proposto` e `cancelado` **não são falha**: são o kill switch, o cooldown, a
falta de endpoint, uma confirmação recusada ou uma proposta esperando o botão. Tratá-los
como erro faria o painel gritar exatamente quando as travas funcionaram.

**Escrita em todo caminho.** Nenhuma ação chega ao `admin_api` sem passar por aqui antes ou
depois: `pause`/`resume` autônomos ([§2](#2-as-três-travas)), proposta, confirmação,
cancelamento e recusa ([#18](acoes-confirmadas.md#6-auditoria)). Quando o Turso está fora, a
linha vai para o log em nível ERROR — degradação prevista, com rastro.

**Retenção longa, fora da limpeza do #9.** O job de retenção (`turso/retention.py`) só
apaga da tabela `logs`; `action_audit` e `action_confirmations` estão em `PROTECTED_TABLES`
e um teste trava isso. Log de INFO envelhece, auditoria de ação não: "quem reiniciou o
agents-platform em julho?" é uma pergunta que se faz em dezembro.

### Como consultar

```bash
/acoes                # no Telegram: as 10 últimas ações, legíveis no celular (#19)
make acoes            # o mesmo, por linha de comando
make actions-status   # kill switch, pausas vencidas e as 10 últimas ações
make actions-sweep    # despausa agora tudo que já venceu
make confirm-pending  # propostas aguardando confirmação (#18)
```

O `/acoes` responde uma linha por ação — desfecho, o quê, em quem, por ordem de quem:

```
🗂 Últimas 3 ações
⏳ 07-24 07:14 restart_service agents-platform · telegram:lucas
    aguardando confirmação
✅ 07-24 07:06 pause_tool search · ops-centro
    HTTP 200
🚫 07-24 06:58 pause_tool fetch · ops-centro
    kill switch ligado (AUTONOMOUS_ACTIONS=off)
```

## 7. Simulação (o critério de aceite)

Sem depender de a métrica real ultrapassar o limiar:

```bash
# 1. semear falhas da mesma tool no Turso (o que o app faria ao falhar)
python - <<'PY'
import libsql, os
from datetime import datetime, timezone
conn = libsql.connect(database=os.environ["TURSO_DATABASE_URL"],
                      auth_token=os.environ.get("TURSO_AUTH_TOKEN"))
agora = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
conn.executemany(
    "INSERT INTO logs (timestamp, app_name, tenant_id, trace_id, level, message, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    [(agora, "agents-platform", "acme", None, "ERROR", "tool search: timeout",
      '{"tool": "search"}')] * 6)
conn.commit()
PY

# 2. disparar o webhook com a label `tool` (o mesmo formato do Grafana)
curl -sS -X POST https://<domínio>/alerts/grafana \
  -H "X-Alert-Token: $ALERT_WEBHOOK_TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"firing","alerts":[{"status":"firing","labels":{
        "alertname":"Agents Platform: taxa de erro por tool MCP acima de 10%",
        "app_name":"agents-platform","tenant_id":"acme","environment":"prod",
        "severity":"warning","tool":"search"},"annotations":{"summary":"tool search falhando"}}]}'

# 3. conferir: a resposta traz `actions`, o Telegram recebe a mensagem e o audit tem a linha
make actions-status
```

Para provar a despausa sem esperar 15 minutos, use `AUTONOMOUS_PAUSE_TTL_SECONDS=60` e
rode `make actions-sweep` depois de um minuto.

> **Estado (2026-07-24):** simulação verificada localmente ponta a ponta (falhas semeadas →
> webhook → `POST /admin/tools/pause` com TTL → linha em `action_audit` → mensagem no canal
> do Hermes; sweep despausando e sendo idempotente na segunda passada). Em produção a ação
> fica em `bloqueado` até os apps exporem `pause_tool` — é o lado cross-repo da issue.
