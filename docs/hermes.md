# Canal Hermes: notificação e consulta sob demanda (issues #15 e #16)

> Código: [`ops_centro/receiver/hermes.py`](../ops_centro/receiver/hermes.py) (envio) e
> [`ops_centro/receiver/status.py`](../ops_centro/receiver/status.py) (consultas) ·
> endpoints em [`ops_centro/receiver/app.py`](../ops_centro/receiver/app.py).

Os dois sentidos do canal entre o receiver e o Hermes:

```
Grafana ──webhook──▶ receiver ──enriquece (#14)──▶ POST HERMES_WEBHOOK_URL ──▶ Telegram
                        │                                    │
                        │                          falhou 3x │
                        │                                    ▼
                        │                        hermes_dead_letter (Turso)
                        │
   Telegram ──"/status"──▶ Hermes ──POST /hermes/consulta──▶ receiver ──▶ Mimir + Turso
```

## 1. Contrato receiver→Hermes (#15)

`POST` no `HERMES_WEBHOOK_URL`, JSON, autenticado com `HERMES_WEBHOOK_TOKEN` nos dois
formatos (`X-Hermes-Token` e `Authorization: Bearer`) — o mesmo padrão do salto anterior.

```json
{
  "version": 1,
  "source": "ops-centro",
  "source_version": "0.1.0",
  "sent_at": "2026-07-24T06:47:09+00:00",
  "kind": "alert",                       // alert | action (#17)
  "status": "firing",                    // firing | resolved | executed
  "severity": "warning",
  "group_key": "alert/<título>/<app>/<tenant>",
  "title": "Tool search falhando em 23.4% das chamadas",
  "text": "⚠️ *Tool search…*",            // MarkdownV2 pronto para o sendMessage
  "parse_mode": "MarkdownV2",
  "app_name": "agents-platform",
  "tenant_id": "acme",
  "trace_id": "4bf92f…",
  "environment": "prod",
  "links": {"dashboard": "…", "trace": "…", "runbook": "…"},
  "alerts": [ { …payload enriquecido do #14… } ],
  "suppressed": 0,                       // notificações seguradas pelo rate limit
  "buttons": []                          // teclado inline, só em proposta de ação (#18)
}
```

**Por que a mensagem já vem formatada.** O `text` é MarkdownV2 com emoji de severidade
(🚨 crítico, ⚠️ warning, ✅ resolvido, 🤖 ação autônoma), logs em bloco de código e links
clicáveis. A formatação mora aqui, e não no Hermes, porque é aqui que vivem o schema, o
enriquecimento e os testes — o handler do Hermes fica sendo o que ele deve ser: um relay.
Quem quiser reformatar tem `alerts[]` e os campos estruturados no mesmo envelope.

**Por que MarkdownV2 e não HTML:** é o formato que o Telegram documenta como atual. O
preço é o escape agressivo (`escape_md`) — escapar de menos é `400 Bad Request` no
`sendMessage`, o erro mais chato de descobrir em produção. Log de erro vai em bloco de
código justamente para não passar pelo parser.

**`version`** existe para o dia em que o formato mudar: o Hermes recusa e loga o que não
souber ler, em vez de mandar mensagem quebrada.

### Entrega: retry, dead-letter, rate limit

| Comportamento | Default | Env var |
| --- | --- | --- |
| Tentativas | 3 | `HERMES_RETRIES` |
| Backoff inicial (dobra a cada tentativa, com jitter) | 0,5s | `HERMES_BACKOFF` |
| Timeout por tentativa | 5s | `HERMES_TIMEOUT` |
| Rate limit (balde de fichas) | 10 msg / 60s | `HERMES_RATE_LIMIT` / `HERMES_RATE_WINDOW` |

- **5xx e 429 voltam; 4xx não.** Token errado não melhora na terceira tentativa — vai
  direto para o dead-letter, com o motivo.
- **Esgotadas as tentativas**, o envelope inteiro vai para `hermes_dead_letter` no Turso
  (migration [`0003`](../db/migrations/0003_hermes_dead_letter.sql)), pronto para reenvio.
  É o critério de aceite da issue: queda do Hermes não perde alerta em silêncio. Se o
  Turso também estiver fora, o envelope sai no log em nível ERROR — pior que não entregar
  é não haver rastro.
- **Rate limit** é anti-tempestade, e o que ele segurou é anunciado na próxima mensagem
  que passar (`+N notificação(ões) suprimida(s)`). Silenciar sem dizer que silenciou é
  como se perde a confiança no canal. O agrupamento da notification policy (#12) já reduz
  o volume antes daqui.
- **Nada disso levanta:** o webhook do Grafana responde 202 mesmo com o Telegram fora.

Métricas do próprio envio (catálogo em [`metrics.py`](../ops_centro/metrics.py)):
`ops_centro_hermes_notifications_total{status}` e
`ops_centro_hermes_delivery_duration_seconds` — uma subida de `status="error"` é o aviso
de que o Telegram parou de receber.

### O que fica do lado do Hermes

Repo [hermes-dash](https://github.com/CidLucas), não este. O handler precisa de:

1. rota `POST` autenticada que aceite o envelope acima;
2. `sendMessage` com `chat_id` do canal, `text` e `parse_mode` vindos do payload;
3. resposta 2xx **depois** de aceitar (não depois de o Telegram confirmar) — o retry daqui
   é para indisponibilidade dele, não para lentidão da API do Telegram;
4. para `kind: "action"` (#17), o mesmo caminho: a mensagem já explica o que foi feito;
5. quando vier `buttons` (#18), montar o `reply_markup.inline_keyboard` com ele e devolver o
   `callback_data` do botão tocado ao `POST /hermes/confirmacao` — ver
   [acoes-confirmadas.md §3](acoes-confirmadas.md#3-o-que-o-hermes-precisa-fazer).

## 2. Consultas sob demanda (#16)

`POST /hermes/consulta` no receiver, autenticado com o **mesmo `ALERT_WEBHOOK_TOKEN`** do
webhook (mesmo header, mesma comparação `compare_digest`, mesmo 503 quando não há token
configurado). Corpo: `{"command": "/status acme"}` — o texto cru que chegou no Telegram.

| Comando | O que responde | De onde |
| --- | --- | --- |
| `/status` | volume, % de erro e p95 dos dois apps na última hora | Mimir (3 queries por app) + Turso (erros) |
| `/status <tenant>` | volume e % de erro do tenant | counters de volume por tenant |
| `/erros [hoje\|1h\|30m]` | contagem por app/nível + últimas 5 linhas | Turso |
| `/acoes` | as 10 últimas ações do Hermes, com desfecho e motivo (#19) | `action_audit` no Turso |
| `/reiniciar <app>` | **proposta** de restart com botões (#18) | — (não executa nada) |
| `/despausar <tool>` | **proposta** de desfazer uma pausa (#18) | pausa vigente em `action_audit` |
| `/ajuda` | a lista acima | — |

Resposta: `{"command", "text" (MarkdownV2), "parse_mode", "data"}` — mais `buttons` nos dois
comandos de ação. Comando desconhecido devolve **200 com a ajuda** — quem digitou errado no
Telegram quer a lista de comandos, não um código de erro.

Os apelidos são os que se digitam de verdade: `/saude`, `/ações`, `/auditoria`, `/restart`,
`/retomar` caem nos comandos acima.

**Nada nesta rota executa.** `/reiniciar` e `/despausar` devolvem uma proposta com token de
uso único; quem executa é o `POST /hermes/confirmacao`, depois do botão — ver
[acoes-confirmadas.md](acoes-confirmadas.md). O corpo da consulta ganhou `chat_id` e `user`
por causa disso: quem pode propor é decidido pelo chat, não pelo texto da mensagem.

**Limite de custo (tarefa da issue).** Toda pergunta vira um conjunto fechado de queries
agregadas: `sum(increase(...))`, `histogram_quantile(0.95, sum by (le) ...)` e duas
agregações com `LIMIT` no Turso. O único `by (...)` permitido é `by (le)`; não existe
caminho aqui para varredura de traces no Tempo, que é o que estoura o free tier justamente
no dia do incidente. O teste `test_queries_sao_agregadas_e_nao_tocam_o_tempo` trava isso.

**Sem p95 por tenant** de propósito: `tenant_id` só existe nas métricas de volume (regra de
cardinalidade de [`conventions.py`](../ops_centro/conventions.py)). A resposta por tenant
diz isso em vez de inventar o número.

**Orçamento de tempo:** `STATUS_QUERY_TIMEOUT` (default 8s, contra o critério de <10s).
Estourou, a resposta é "a consulta passou de 8s" — não um cliente pendurado. Parte que
falhou (Grafana ou Turso) vira "sem dados"/"indisponível" e o resto chega.

## 3. Fluxo de trabalho

```bash
make hermes-sample    # envelope de exemplo + mensagem renderizada
make hermes-send      # manda a notificação sintética ao HERMES_WEBHOOK_URL
make status           # responde '/status' localmente, como o Hermes veria
make erros            # '/erros hoje'
make acoes            # '/acoes' — histórico do audit log (#19)
```

Configuração em [`.env.example`](../.env.example) (seções "Hermes / Telegram" e "Consultas
sob demanda"); mapa de segredos em [secrets.md](secrets.md).

O dead-letter (e o audit de ações do #17) vive em tabelas novas: rode **`make migrate`**
uma vez no ambiente antes de subir esta versão. Sem elas, uma notificação não entregue cai
no log em vez do banco — degradação prevista, mas é a prova que se quer ter.

## 4. Teste ponta a ponta

Roteiro do critério de aceite ("alerta disparado no Grafana chega no Telegram em < 1 min"):

1. **canal isolado** — `make hermes-send`. Manda um alerta sintético direto ao Hermes; se
   a mensagem aparecer no Telegram, o salto receiver→Hermes→Telegram está de pé;
2. **fluxo completo** — no Grafana Cloud, *Alerting → nome da regra → Preview/Test* (ou
   baixe temporariamente o limiar de uma regra em `ops_centro/grafana/alerts.py` e rode
   `make alerts-apply`). O caminho exercitado é o de produção: regra → contact point →
   `POST /alerts/grafana` → enriquecimento → Hermes → Telegram;
3. **queda do Hermes** — derrube o Hermes e dispare de novo: o webhook continua
   respondendo 202, e a linha aparece em `hermes_dead_letter`:

   ```sql
   SELECT created_at, reason, attempts FROM hermes_dead_letter ORDER BY created_at DESC LIMIT 5;
   ```

4. **consulta** — `/status` no Telegram (ou
   `curl -H "X-Alert-Token: $ALERT_WEBHOOK_TOKEN" -d '{"command":"/status"}' \
   https://<domínio>/hermes/consulta`).

> **Estado (2026-07-24):** verificado localmente de ponta a ponta com um Hermes falso —
> webhook do Grafana → enriquecimento no Turso → envelope entregue (`HTTP 200`, 1
> tentativa) → ação autônoma de pausa (#17) anunciada no mesmo canal → `/erros hoje`
> respondendo pelo `POST /hermes/consulta`. Falta o handler do lado do Hermes real e o
> disparo a partir do Grafana Cloud (§4.2).
