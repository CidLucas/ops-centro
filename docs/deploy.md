# Deploy do receiver na EC2 do Hermes (issue #13)

> Arquivos: [`deploy/`](../deploy/) · imagem: publicada pelo
> [CD](../.github/workflows/cd.yml) no GHCR · vigia externo:
> [healthcheck.yml](../.github/workflows/healthcheck.yml).

O receiver mora na mesma EC2 do Hermes — custo já assumido (§11 do plano), e é lá que o
Hermes está para receber o alerta enriquecido (#15). Este documento é o caminho completo,
do zero ao webhook de produção chegando com 202.

## 1. Decisão de entrega: GHCR, não build na EC2

O CD builda a imagem no runner, roda o smoke (entrypoints importam, roda non-root,
`/healthz` responde) e **só então** publica no GHCR. A EC2 faz `pull`, nunca `build`.

Por quê: build na EC2 gastaria a CPU da máquina que precisa estar de pé para receber
alerta, e faria o deploy depender do git e do compilador na instância. Com registry, o
rollback é trocar uma tag — e a imagem que entra em produção é exatamente a que passou no
smoke.

Tags publicadas em `ghcr.io/cidlucas/ops-centro-receiver`:

| Tag | Quando |
| --- | --- |
| `latest` | todo push na `main` |
| `sha-<commit>` | sempre — é a tag que se fixa para congelar versão ou voltar atrás |
| `v1.2.3`, `1.2` | tags de release `v*` |

O pacote nasce privado. Torne-o público (*Package settings → Change visibility*) **ou**
faça `docker login ghcr.io` na EC2 com um PAT de `read:packages` — sem um dos dois, o
`pull` falha com `denied`.

## 2. Pré-requisitos na EC2

```bash
docker --version && docker compose version   # Docker Engine + plugin compose
sudo systemctl is-enabled docker             # precisa estar enabled: é o que garante o
                                             # `restart: unless-stopped` após reboot
aws sts get-caller-identity                  # IAM role com ssm:GetParametersByPath + kms:Decrypt
```

Security group: **443/tcp e 80/tcp abertos para 0.0.0.0/0**. O 80 não é opcional se o
Caddy for emitir certificado (desafio HTTP-01 do Let's Encrypt). A 8080 fica fechada — o
container publica só em `127.0.0.1`.

DNS: um registro A do domínio do receiver (ex.: `ops.seudominio.com`) para o IP elástico
da instância, **antes** de subir o Caddy.

## 3. Segredos: SSM Parameter Store, nunca arquivo commitado (RNF06)

```bash
aws ssm put-parameter --type SecureString --name /ops-centro/prod/ALERT_WEBHOOK_TOKEN \
  --value "$(openssl rand -hex 32)"
aws ssm put-parameter --type SecureString --name /ops-centro/prod/TURSO_DATABASE_URL --value 'libsql://...'
aws ssm put-parameter --type SecureString --name /ops-centro/prod/TURSO_AUTH_TOKEN --value '...'
aws ssm put-parameter --type SecureString --name /ops-centro/prod/OTEL_EXPORTER_OTLP_HEADERS --value 'Authorization=Basic%20...'
aws ssm put-parameter --type String --name /ops-centro/prod/OTEL_EXPORTER_OTLP_ENDPOINT --value 'https://otlp-gateway-prod-sa-east-1.grafana.net/otlp'
aws ssm put-parameter --type String --name /ops-centro/prod/GRAFANA_STACK_URL --value 'https://<slug>.grafana.net'
aws ssm put-parameter --type String --name /ops-centro/prod/ENVIRONMENT --value 'prod'
aws ssm put-parameter --type String --name /ops-centro/prod/RECEIVER_DOMAIN --value 'ops.seudominio.com'
aws ssm put-parameter --type String --name /ops-centro/prod/ACME_EMAIL --value 'voce@exemplo.com'
```

`GRAFANA_STACK_URL` não é decoração: é dele que sai o link do trace e do dashboard na
mensagem enriquecida (#14). Sem ele o alerta chega com logs, mas sem links.

Na EC2:

```bash
git clone https://github.com/CidLucas/ops-centro.git /opt/ops-centro   # ou só o deploy/
cd /opt/ops-centro/deploy
./env-from-ssm.sh          # escreve ./.env (modo 600) a partir de /ops-centro/prod
```

Rotação de qualquer segredo = `put-parameter` + `./env-from-ssm.sh` + `./deploy.sh`. O
`.env` é derivado, nunca editado à mão.

## 4. Subir

```bash
cd /opt/ops-centro/deploy
./deploy.sh --proxy        # pull + up -d + espera o /healthz; --proxy sobe o Caddy junto
```

O script recusa subir sem `.env`, avisa se ele não estiver em 600, e **só declara sucesso
depois que o `/healthz` responde** — container "Up" não é serviço de pé. Falhou, ele
imprime o log e a linha do rollback.

Se o Hermes já tem um proxy na máquina (nginx/caddy fora deste compose), **não** use
`--proxy`: aponte o proxy existente para `127.0.0.1:8080` e mantenha as duas rotas
(`/alerts/grafana` e `/healthz`) como as únicas públicas. Exemplo em nginx:

```nginx
location /alerts/grafana { proxy_pass http://127.0.0.1:8080; }
location /healthz        { proxy_pass http://127.0.0.1:8080; }
```

Reboot da instância: o `restart: unless-stopped` do compose devolve o serviço sozinho,
desde que o daemon do Docker esteja `enabled` (§2).

## 5. Conferir

```bash
curl -fsS https://ops.seudominio.com/healthz | jq        # 1. HTTPS público de pé
docker compose ps                                        # 2. estado e health do container
docker compose logs -f receiver                          # 3. o que ele está vendo
```

Webhook de verdade, do próprio Grafana Cloud: *Alerting → Contact points →
`ops-centro-hermes` → **Test***. O receiver responde `202` e o log mostra
`alerta recebido do Grafana` seguido de `alerta enriquecido` — os dois critérios de aceite
do #13 numa tacada só. Ver [alertas.md §6](alertas.md#6-teste-de-ponta-a-ponta).

Depois de o endpoint estar no ar, configure a *repository variable*
`RECEIVER_HEALTH_URL=https://ops.seudominio.com/healthz`: é o que liga o
[healthcheck.yml](../.github/workflows/healthcheck.yml), que bate no endpoint de meia em
meia hora **de fora**, pelo mesmo caminho do Grafana, e ainda avisa quando o certificado
está a menos de 7 dias do vencimento.

```bash
gh variable set RECEIVER_HEALTH_URL --body 'https://ops.seudominio.com/healthz'
```

## 6. Atualizar e voltar atrás

```bash
./deploy.sh                    # pega a `latest` (o que a main tem agora)
./deploy.sh sha-abc123def      # fixa um commit específico
```

Rollback é `./deploy.sh <sha anterior>` — as tags estão em
[Packages](https://github.com/CidLucas/ops-centro/pkgs/container/ops-centro-receiver). Para
congelar a versão de forma persistente, ponha `RECEIVER_IMAGE` no Parameter Store: o
`.env` regerado passa a trazer a tag fixa.

## 7. O observador também é observado

O receiver exporta a própria telemetria pelo mesmo OTLP dos apps (`setup_observability` no
`ops_centro/receiver/app.py`), com `app_name=ops-centro` e `environment=prod`. O que
aparece no Grafana:

| Sinal | Onde ver |
| --- | --- |
| `ops_centro_alerts_received_total` | painel "Alertas recebidos" em `ops-centro-visao-geral` |
| `ops_centro_alert_enrichment_total{status}` | subida de `error` = alertas saindo sem contexto |
| `ops_centro_alert_enrichment_duration_seconds` | encostou no deadline? o Turso está lento |
| logs do container | `docker compose logs` (JSON estruturado) e Loki, via OTLP |

Três camadas de vigilância, cada uma pegando o que a anterior não pega: o healthcheck do
compose reinicia container morto; o `healthcheck.yml` vê o que está entre o Grafana e o
container (DNS, TLS, security group, proxy); as métricas mostram degradação sem queda.

## 8. Problemas conhecidos

| Sintoma | Causa provável |
| --- | --- |
| `pull` com `denied` | pacote privado no GHCR e sem `docker login` na EC2 (§1) |
| `/healthz` local ok, público não | 443 fechada no security group, DNS não propagado, ou Caddy sem a porta 80 para o ACME |
| Webhook responde 503 | `ALERT_WEBHOOK_TOKEN` ausente no `.env` — falha fechada de propósito |
| Webhook responde 401 | token do contact point diferente do da EC2; regenere e rode `make alerts-apply` com o mesmo valor |
| Alerta chega sem logs | Turso indisponível ou sem `TURSO_DATABASE_URL` — ver `strategy` no payload ([alertas.md §4](alertas.md#4-enriquecimento-issue-14)) |
| Certificado não emite | porta 80 fechada, DNS apontando para outro IP, ou rate limit do Let's Encrypt (o volume `caddy_data` existe justamente para não repetir emissão a cada restart) |
