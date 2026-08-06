# Issue #15 (correção) — Entrega direta à Bot API do Telegram (bot do ops-centro)

## Contexto

Repo: `/tmp/ops-centro` (CidLucas/ops-centro), branch main em `e956697` (working tree limpo).

**Problema reportado (06/08/2026):** o alerta de ponta a ponta chega ao Telegram, mas
**pelo bot errado**. O fluxo atual é: Grafana → receiver (enriquecimento) → `POST
HERMES_WEBHOOK_URL` → rota `ops-centro-alerts` do gateway do Hermes (porta 8644) →
`direct-deliver` → Telegram. O gateway entrega SEMPRE com o bot do perfil default do
Hermes (`TELEGRAM_BOT_TOKEN` do `.env` do Hermes). O usuário criou um bot dedicado do
ops-centro (`@KnowledgeBaseCurator_bot`, token em `TELEGRAM_BOT_TOKEN` no `.env` do
deploy) e quer que os alertas cheguem POR ESSE BOT.

**Causa raiz (verificada):** `_deliver_cross_platform` no gateway do Hermes chama
`adapter.send()`, e o adapter do Telegram usa o único `TELEGRAM_BOT_TOKEN` do gateway
(o bot principal). O gateway não tem conceito de segundo bot — a apikey do bot do
ops-centro que o usuário passou foi parar no `.env` do deploy, usada pelo contact point
Telegram nativo do Grafana (#25), mas o relay do Hermes nem olha para ela.

**Validação ao vivo (feita, funcionou):** POST direto à Bot API
`https://api.telegram.org/bot<TOKEN>/sendMessage` com `chat_id`, `text` MarkdownV2 e
`parse_mode=MarkdownV2` entregou no bot do ops-centro (`@KnowledgeBaseCurator_bot`,
msg_ids 808/809, chat Lucas/cidlucas 8607712655). O receptor já tem o token no
ambiente (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` no `.env`, injetado pelo compose).

## Solução

O receiver passa a entregar **direto à Bot API do Telegram** com o token do bot do
ops-centro quando `TELEGRAM_BOT_TOKEN` está configurado. O caminho do webhook do Hermes
(`HERMES_WEBHOOK_URL` + HMAC) vira **fallback** quando o token de bot NÃO está
configurado (não quebrar quem ainda usa o relay).

```
antes: receiver → gateway 8644 (bot principal) → você
depois: receiver → https://api.telegram.org/bot<TOKEN>/sendMessage (bot do ops-centro) → você
       (fallback) receiver → HERMES_WEBHOOK_URL (relay legado)
```

Tudo que o relay fazia já existe no receiver: retry com backoff, rate limit e
dead-letter no Turso. O gateway sair do caminho de entrega ainda **remove um ponto de
falha** da cadeia de alerta.

## Tarefa — TDD vertical (1 teste RED → 1 GREEN por ciclo)

Edite **apenas**: `ops_centro/receiver/hermes.py`, `tests/test_hermes.py`,
`.env.example` e `docs/hermes.md`. Não toque em outros módulos do receiver.
NÃO rode deploy/`--apply` (publicação é passo posterior com revisão humana).

### 1. Testes novos em `tests/test_hermes.py` (RED primeiro)

Adicione, seguindo o estilo dos existentes (fixtures `transporte`, `sem_espera`,
`conectar`, `banco`, `limitador_limpo`):

- `test_entrega_telegram_direto_na_bot_api(monkeypatch)`: setenv
  `TELEGRAM_BOT_TOKEN="123:ABC"` e `TELEGRAM_CHAT_ID="8607712655"`; `deliver()` sem
  `url` explícita → POST para `https://api.telegram.org/bot123:ABC/sendMessage`; corpo
  JSON tem `chat_id == "8607712655"`, `text == notificacao.text` (MarkdownV2),
  `parse_mode == "MarkdownV2"`; status `entregue`, attempts 1. NENHUM header de auth
  (sem HMAC no caminho da Bot API).
- `test_telegram_usa_url_do_token_do_bot(monkeypatch)`: sem `HERMES_WEBHOOK_URL`, só
  com `TELEGRAM_BOT_TOKEN` → destino é a Bot API (não DISABLED).
- `test_telegram_buttons_viram_inline_keyboard(monkeypatch)`: notificação com
  `buttons=(({"text":"Confirmar","callback_data":"ok"},),)` → corpo tem
  `reply_markup.inline_keyboard == [[{"text":"Confirmar","callback_data":"ok"}]]`.
- `test_telegram_4xx_nao_e_retentado_e_vai_ao_dead_letter(monkeypatch, conectar)`:
  transporte([401]) com TELEGRAM_BOT_TOKEN setado → 1 chamada, status dead-letter.
- `test_telegram_5xx_e_retentado(monkeypatch)`: transporte([503, 200]) → 2 chamadas,
  entregue com attempts 2.
- `test_telegram_sem_token_cai_no_fallback_do_webhook(monkeypatch)`: sem
  `TELEGRAM_BOT_TOKEN`, com `HERMES_WEBHOOK_URL` → POST para a URL do webhook, com
  headers HMAC (comportamento atual preservado).
- `test_sem_telegram_e_sem_webhook_vira_no_op(monkeypatch)`: sem ambos → DISABLED.

Os testes existentes que passam `url="https://hermes/notify"` explícita DEVEM
continuar passando sem alteração (URL explícita = caminho webhook legado).

### 2. Implementação em `ops_centro/receiver/hermes.py`

- Nova função `telegram_url() -> str`: lê `TELEGRAM_BOT_TOKEN`; se vazio, retorna "";
  senão `f"https://api.telegram.org/bot{token}/sendMessage"`.
- Nova função `_telegram_body(notification) -> dict`: `{"chat_id":
  TELEGRAM_CHAT_ID, "text": notification.text, "parse_mode": "MarkdownV2"}`; se
  `notification.buttons`, adiciona `"reply_markup": {"inline_keyboard":
  [[{"text": b.get("text",""), "callback_data": b.get("callback_data", b.get("id",""))}
  for b in linha] for linha in notification.buttons]}`.
- Em `deliver()`: a resolução do destino passa a ser:
  1. `url` explícita (parâmetro) → caminho webhook legado (comportamento atual, HMAC);
  2. senão, `telegram_url()` não vazio → caminho Bot API (sem headers de auth);
  3. senão, `webhook_url()` → caminho webhook;
  4. senão → DISABLED (`TELEGRAM_BOT_TOKEN e HERMES_WEBHOOK_URL ausentes`).
  O loop de retry/backoff/rate-limit/dead-letter e as métricas `_record` são os MESMOS
  para os dois caminhos — só mudam corpo (`_telegram_body` vs `as_payload`), headers
  e destino. Ajuste o `detail` do DISABLED e as mensagens de log para citar o caminho
  usado (ex: "notificação entregue ao Telegram" vs "ao Hermes").
- Mantenha `webhook_url()`, `_auth_headers()` e o contrato `as_payload()` intactos
  (fallback e testes legados dependem deles).

### 3. `.env.example`

Na seção `# --- Hermes / Telegram ---`, documente a nova ordem:
- `TELEGRAM_BOT_TOKEN` (caminho primário — bot dedicado do ops-centro) e
  `TELEGRAM_CHAT_ID`; vazio = usa o fallback do webhook.
- Mantenha `HERMES_WEBHOOK_URL`/`HERMES_WEBHOOK_TOKEN` documentados como fallback
  legado (relay no gateway).

### 4. `docs/hermes.md`

- Atualize o diagrama do topo (linha ~10) para refletir o caminho direto à Bot API.
- Reescreva a seção `## 6. Relay no Hermes` para: **caminho primário = Bot API direta**
  (endpoint, chat_id, parse_mode, retry/rate-limit/dead-letter no receiver — agora com
  a observabilidade do `delivery.status` vindo da resposta da Bot API); o relay no
  gateway vira **fallback opcional** (quando não há token de bot), com o wiring HMAC
  resumido.
- Atualize o bloco `> **Estado (2026-08-06)**` e o `make hermes-send` da §5 se citar
  o caminho.

## Critérios de aceite

1. `uv run python -m pytest tests/test_hermes.py -q` — tudo verde (novos + legados).
2. `uv run python -m pytest tests/ -q` — nada mais quebrou.
3. `uv run ruff check ops_centro tests` — limpo.
4. `grep -c "api.telegram.org" ops_centro/receiver/hermes.py` ≥ 1 e os testes novos
   cobrem o caminho Bot API.
5. `.env.example` e `docs/hermes.md` refletem o novo caminho primário.
6. NÃO rodar deploy, NÃO tocar em outros módulos do receiver, NÃO tocar no gateway do
   Hermes (a rota `ops-centro-alerts` fica como está, só deixa de ser usada).
