# Issue #25 — Rota de notificação que não dependa da EC2 do Hermes (dependência circular)

## Contexto

Repo: `/tmp/ops-centro` (CidLucas/ops-centro). Incidente de 05/08/2026: a EC2 do
Hermes caiu (loop de restart → memória/swap exaurida → máquina inacessível). O fluxo
de alerta atual é circular: Grafana Cloud → contact point `webhook` → **receiver na
EC2** → Hermes → Telegram. Se a EC2 cai, o alerta de "coletor de host parado"
(`ops-centro-host-coletor-parado`, critical) nunca chega — o receiver caiu junto.

A correção (issue #25): **contact point Telegram nativo do Grafana Cloud** + rota
específica para alertas de infra (`component="ec2-host"`) que vai DIRETO ao Telegram,
sem passar pela EC2. O Grafana Cloud é gerenciado — o bot dele continua vivo quando a
nossa EC2 morre. O dead-man's switch do host (#27) só é útil com esta rota.

## Tarefa

Edite **apenas** `ops_centro/grafana/alerts.py` e `tests/test_alerts.py`, e re-genere
`grafana/alerts/roteamento.yaml` com `--write`. **NÃO rode `--apply`** (publicação é
passo posterior, com revisão humana).

### 1. Constantes novas (junto de RECEIVER_NAME/RECEIVER_UID, ~linha 56)

```python
TELEGRAM_NAME = "ops-centro-telegram"
TELEGRAM_UID = "ops-centro-telegram-bot"
```

### 2. Novo contact point: `build_telegram_contact_point()`

Mesma forma de `build_contact_point()` (linha 807), com `type: "telegram"` e settings:

```python
{
    "orgId": 1,
    "name": TELEGRAM_NAME,
    "receivers": [
        {
            "uid": TELEGRAM_UID,
            "type": "telegram",
            "disableResolveMessage": False,
            "settings": {
                "bottoken": "${TELEGRAM_BOT_TOKEN}",
                "chatid": "${TELEGRAM_CHAT_ID}",
            },
        }
    ],
}
```

`bottoken`/`chatid` são os nomes de settings que o Grafana usa para contact points
Telegram (confira na doc oficial do Grafana Alerting se preciso). Os valores são
**placeholders** — o token do bot nunca entra no repo (RNF06, mesmo padrão do
`${ALERT_WEBHOOK_TOKEN}`); o `--apply` resolve do ambiente.

### 3. `build_routing()` (linha 858): incluir os DOIS contact points

```python
def build_routing() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "contactPoints": [build_contact_point(), build_telegram_contact_point()],
        "policies": [build_policy()],
    }
```

### 4. `build_policy()` (linha 834): rota de infra ANTES da rota critical

A ordem importa: o Grafana usa a PRIMEIRA rota que casa (sem `continue: true`). Os
alertas de host critical (`restart-loop`, `coletor-parado`) também casariam na rota
`severity=critical` atual — então a rota `component=ec2-host` **vem primeiro**:

```python
"routes": [
    {
        # Infra: direto ao Telegram nativo do Grafana Cloud — NÃO depende da EC2.
        "receiver": TELEGRAM_NAME,
        "object_matchers": [["component", "=", "ec2-host"]],
        "group_wait": "10s",
        "group_interval": "5m",
        "repeat_interval": "1h",
        "continue": False,
    },
    {
        "receiver": RECEIVER_NAME,
        "object_matchers": [["severity", "=", SEVERITY_CRITICAL]],
        "group_wait": "10s",
        "group_interval": "5m",
        "repeat_interval": "1h",
        "continue": False,
    },
],
```

### 5. `apply_all` / `apply_contact_point` (linhas ~1098-1141)

Hoje `apply_contact_point()` aplica só `build_contact_point()`. Faça-o aplicar os
**dois** contact points (webhook + telegram), idempotente, com o mesmo padrão de
`[OK]`/`[FALHA]`. Confira se o endpoint de contact points do Grafana aceita
`bottoken` em `settings` via API; se exigir `secureSettings`, use o campo correto
(no provisionamento YAML pode ficar como settings normal com placeholder — o teste de
segredo continua valendo porque o placeholder não é o token).

### 6. Resolução de placeholders no `--apply` (linhas ~1020-1023)

Adicione ao dicionário de placeholders:
```python
"TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
"TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
```
E, na validação de env obrigatório (linha ~1062), exija `TELEGRAM_BOT_TOKEN` e
`TELEGRAM_CHAT_ID` também (o contact point telegram sem token não tem como ser
criado — falha fechada, como o ALERT_WEBHOOK_TOKEN).

### 7. Testes (`tests/test_alerts.py`)

- Atualize o teste do contact point: `build_routing()["contactPoints"]` tem 2 itens;
  o webhook continua com placeholder `${ALERT_WEBHOOK_TOKEN}` e URL `${RECEIVER_WEBHOOK_URL}`;
  o telegram tem `type == "telegram"` e settings `bottoken`/`chatid` com placeholder
  (começam com `${` — nunca o valor real).
- Teste de rota: a primeira rota de `build_policy()["routes"]` é a de infra
  (`object_matchers` contém `component` e o receiver é `TELEGRAM_NAME`); a segunda
  continua sendo a de critical → webhook.
- `test_todos_os_arquivos_existem_no_repo` continua igual (roteamento.yaml já existe).

## Critérios de aceite

1. `uv run python -m ops_centro.grafana.alerts --check` passa (roteamento.yaml em dia).
2. `uv run python -m pytest tests/ -q` — tudo verde (`uv run ruff check ops_centro tests`).
3. `grafana/alerts/roteamento.yaml` contém os 2 contact points + a rota de infra
   ANTES da de critical, tudo com placeholders `${...}` (grep por `glc_`/`glsa_`/token → zero).
4. Nenhuma mudança em `apps.yaml`, `free-tier.yaml`, `turso-retencao.yaml`,
   `host.yaml`, `ops_centro/metrics.py`, `ops_centro/conventions.py`, Makefile, compose.
5. NÃO rodar `--apply`.
