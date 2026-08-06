# Ações com confirmação humana (RF10, issue #18)

> Código: [`ops_centro/receiver/confirmations.py`](../ops_centro/receiver/confirmations.py)
> (fluxo) + [`ops_centro/turso/confirmations.py`](../ops_centro/turso/confirmations.py)
> (tokens) · migration [`0005`](../db/migrations/0005_action_confirmations.sql) ·
> auditoria em [`action_audit`](acoes-autonomas.md#6-auditoria-base-da-issue-19).

A [#17](acoes-autonomas.md) age sozinha porque pausar uma tool é reversível e de baixo
risco. Reiniciar um serviço ou desfazer uma pausa antes da hora não é — são as ações da §8
que exigem um humano no meio. Esta é a mitigação direta do risco §10 do plano: "ação
automatizada com impacto indevido".

```
/reiniciar agents-platform ─▶ receiver propõe ─▶ Telegram: impacto + [Confirmar] [Cancelar]
                                    │                             │
                                    ▼                    tocou em Confirmar
                          action_confirmations                    ▼
                          token de uso único,          POST /hermes/confirmacao
                          10 min de validade      valida ─▶ executa ─▶ relata no thread
```

## 1. As quatro travas

Nada executa sem os quatro sins:

| Trava | Onde mora | O que impede |
| --- | --- | --- |
| **Allowlist de ações** | `CONFIRMABLE` no módulo | Não existe caminho para "execute este comando". Só `restart_service` e `resume_tool`, cada uma com endpoint e corpo fixos |
| **Allowlist de quem confirma** | `HERMES_ALLOWED_CHAT_IDS` | Um bot de Telegram é um endpoint público de fato. **Lista vazia = ninguém** — falha fechada |
| **Token de uso único** | `action_confirmations` no Turso | Callback reenviado (rede ruim no celular) não reinicia duas vezes |
| **Expiração** | `expires_at`, default 10 min | Um "sim" de ontem não autoriza um restart hoje, quando o incidente já é outro |

Mais duas, menores e igualmente deliberadas:

- **quem confirma tem que ser o chat que propôs** — um token vazado não vale em outra
  conversa, nem numa conversa também autorizada;
- **o alvo do restart é fechado** nos dois apps conhecidos. `target` chega do Telegram; um
  alvo livre seria um caminho para reiniciar o que o `admin_api` aceitasse.

**O kill switch `AUTONOMOUS_ACTIONS=off` não vale aqui, de propósito.** Ele desliga o que o
ops-centro faz sozinho. Uma pessoa confirmando um restart às 3h da manhã é exatamente o que
se quer que continue funcionando depois de o automatismo ter sido desligado.

## 2. As ações confirmáveis

| Ação | Comando | Alvo | Impacto anunciado |
| --- | --- | --- | --- |
| `restart_service` | `/reiniciar <app>` | `agents-platform`, `file-memory-mcp` | o processo cai e volta: requisições em voo se perdem e o app fica fora por alguns segundos |
| `resume_tool` | `/despausar <tool>` | tool com pausa vigente | a tool volta antes do fim do TTL; se a causa não passou, os erros voltam junto |

O texto de impacto vai **na mensagem**, antes dos botões. Escrevê-lo é metade do trabalho
da issue: um botão "Confirmar" sem a frase que explica o estrago é só um caminho mais curto
para o mesmo acidente.

`/despausar` só é proposto se houver pausa vigente para a tool — o `app_name` e o `trace_id`
da ação saem do próprio registro da pausa, não do que veio digitado.

## 3. O que o Hermes precisa fazer

Duas metades. A primeira já existe (é o `POST /hermes/consulta` do
[#16](hermes.md#2-consultas-sob-demanda-16)): mandar o texto do Telegram, agora **com
`chat_id` e `user`** no corpo —

```json
{"command": "/reiniciar agents-platform", "chat_id": "-1001234567890", "user": "lucas"}
```

A resposta é a de sempre (`text`, `parse_mode`, `data`) mais um `buttons`, no formato do
teclado inline da Bot API:

```json
{
  "command": "reiniciar",
  "text": "🛑 *Confirmação necessária* …",
  "parse_mode": "MarkdownV2",
  "buttons": [[{"text": "✅ Confirmar", "callback_data": "ops:confirm:Xk3…"},
               {"text": "✖️ Cancelar",  "callback_data": "ops:cancel:Xk3…"}]],
  "data": {"proposed": true, "action": "restart_service", "expires_at": "2026-07-24T07:16:10.000+00:00"}
}
```

A segunda metade é nova: quando o botão for tocado, repassar o `callback_data` **como veio**
para `POST /hermes/confirmacao`, com o mesmo `ALERT_WEBHOOK_TOKEN` dos outros endpoints:

```json
{"callback_data": "ops:confirm:Xk3…", "chat_id": "-1001234567890",
 "user": "lucas", "message_id": 4711}
```

Resposta — **sempre 200**, inclusive recusando:

```json
{"status": "ok", "text": "✅ *Reiniciar agents\\-platform — executado* …",
 "parse_mode": "MarkdownV2", "reply_to_message_id": 4711,
 "data": {"action": "restart_service", "target": "agents-platform", "executed": true}}
```

Um 4xx aqui viraria "o bot não respondeu" na tela de quem acabou de pedir um restart e não
sabe se ele aconteceu — a pior resposta possível. O motivo da recusa vem no `text`, e o
`reply_to_message_id` faz o relato cair no mesmo thread da proposta.

`status` é o vocabulário do audit: `ok` | `error` | `bloqueado` | `cancelado`.

## 4. Contrato com o `admin_api` dos apps (cross-repo)

Além do `pause`/`resume` do [#17](acoes-autonomas.md#3-contrato-com-o-admin_api-dos-apps-cross-repo):

```http
POST <ADMIN_API_..._URL>/admin/service/restart
{"app_name": "agents-platform", "reason": "confirmado por telegram:lucas no Telegram"}
```

Sem URL configurada para o app, a execução vira `bloqueado` no audit e **nada é chamado** —
o estado de hoje, enquanto os endpoints não existem do lado de lá.

## 5. Configuração

```bash
HERMES_ALLOWED_CHAT_IDS=-1001234567890   # vazio = ninguém confirma nada
CONFIRMATION_TTL_SECONDS=600             # default 10 min
```

O `chat_id` de um grupo vem negativo. Para descobrir o seu, mande qualquer mensagem no
grupo e leia o `chat.id` do update no log do Hermes.

## 6. Auditoria

Toda proposta, confirmação, cancelamento e recusa vira linha em `action_audit` (issue #19):

| Momento | `status` | `actor` |
| --- | --- | --- |
| proposta criada | `proposto` | `telegram:<user>` |
| confirmada e executada | `ok` / `error` | `telegram:<user>` |
| cancelada no botão | `cancelado` | `telegram:<user>` |
| recusada (venceu, já usada, chat errado, fora da allowlist) | `bloqueado` | `telegram:<user>` |

Uma recusa que não dá para atribuir a nada (token desconhecido, chat não autorizado) é
auditada com a ação `confirmation` e o alvo `token:<12 primeiros do hash>` — **nunca** o
token em claro. No banco, aliás, só o SHA-256 existe: quem tiver acesso de leitura não
consegue confirmar nada em nome de ninguém.

Consulte pelo Telegram com `/acoes` ou por linha de comando:

```bash
make acoes            # o histórico, como o Hermes responde
make confirm-pending  # propostas ainda válidas + chats autorizados
```

## 7. Simulação (o critério de aceite)

Sem Telegram e sem app de verdade, num banco local:

```bash
export TURSO_DATABASE_URL=./local-logs.db
export HERMES_ALLOWED_CHAT_IDS=-1001234567890
export ADMIN_API_AGENTS_PLATFORM_URL=http://127.0.0.1:9999/admin-api
make migrate

# 1. propor (é o que o /reiniciar no Telegram faz)
make confirm-propose ACAO='restart_service agents-platform'
#   → imprime a mensagem com o impacto, os dois botões e o token

# 2. confirmar dentro dos 10 min
uv run python -m ops_centro.receiver.confirmations --confirm <token>

# 3. conferir a trilha: proposta e execução, nessa ordem
make acoes
```

Para provar a recusa: rode o passo 2 duas vezes (a segunda dá `confirmação já usada`), ou
com `CONFIRMATION_TTL_SECONDS=1` e um `sleep 2` no meio (dá `confirmação vencida`), ou com
`--chat 000` (dá `chat não autorizado`). Cada um deles deixa a linha `bloqueado` no
`make acoes`.

> **Estado (2026-07-24):** fluxo verificado localmente ponta a ponta (proposta → botão →
> execução no `admin_api` falso → relato → audit), incluindo as recusas por expiração, uso
> repetido e chat não autorizado. Falta o lado do Hermes renderizar o teclado inline
> (hermes-dash) e os apps exporem `/admin/service/restart` — os dois lados cross-repo da
> issue.
