# Retenção e limpeza dos logs no Turso (issue #9)

> Código: [`ops_centro/turso/retention.py`](../ops_centro/turso/retention.py) ·
> job: [`.github/workflows/retention.yml`](../.github/workflows/retention.yml) ·
> alertas: [`grafana/alerts/turso-retencao.yaml`](../grafana/alerts/turso-retencao.yaml).
> Pré-requisito: a tabela `logs` do [#8](turso-logs.md).

Risco §10 do plano: *"custo do Turso crescer com volume de logs"*. Tabela append-only sem
política de limpeza cresce até estourar o free tier (~9 GiB) e aí a conta deixa de ser
R$ 0 (RNF01). A mitigação é uma janela de retenção **por nível** — porque o valor de um
log cai com a idade em ritmos muito diferentes conforme a severidade.

## 1. A política

| Nível | Janela | Por quê |
| --- | --- | --- |
| `CRITICAL`, `ERROR` | **90 dias** | material de investigação: reincidência de um erro raro só aparece em janela longa |
| `WARNING` | **30 dias** | serve para tendência ("isso está piorando?"), não para arqueologia |
| `INFO` | **14 dias** | mesma janela do Loki no free tier (RNF03) — passou disso, o log já cumpriu o papel |
| `DEBUG` | **7 dias** | só interessa durante a investigação que o gerou |
| demais | 14 dias | fallback para nível fora do vocabulário |

Sobrescreva por ambiente sem tocar em código:

```bash
TURSO_LOG_RETENTION_DAYS="ERROR=120,WARNING=45,INFO=7"   # o que não aparecer mantém o default
TURSO_LOG_RETENTION_FALLBACK_DAYS=14
```

No workflow, a mesma variável vem de `vars.TURSO_LOG_RETENTION_DAYS` (repository variable,
não secret — não é segredo).

**A política é um limite superior, não uma meta.** O jeito mais barato de não gastar
storage continua sendo não escrever: `INFO` em volume é papel do Loki, com retenção curta
([turso-logs.md → "O que logar"](turso-logs.md#o-que-logar)).

## 2. Rodando à mão

```bash
make retention-dry      # conta o que sairia, por nível — não apaga nada
make retention          # aplica
make retention-vacuum   # aplica + VACUUM (caro, ver abaixo)
```

Saída típica:

```
política: CRITICAL: 90d, DEBUG: 7d, ERROR: 90d, INFO: 14d, WARNING: 30d, demais: 14d
retenção [aplicado] 12840 linha(s) removida(s) (ERROR=310, INFO=11002, WARNING=1528) ·
  418320 → 405480 linhas · banco 214.7 MB (2.2% do teto) · 6.41s
```

Detalhes que importam:

- **Exclusão em lotes** (`--batch`, default 5.000). Transação grande no Turso é o caminho
  mais curto para timeout; lote interrompido só significa que a passada seguinte apaga o
  resto — o job é idempotente por construção.
- **Índice `idx_logs_level_time`** ([migration 0002](../db/migrations/0002_logs_retention.sql))
  existe exatamente para este `DELETE`. Sem ele cada passada é full scan, e row read é
  justamente a cota do free tier que a retenção deveria estar protegendo.
- **`VACUUM` é opt-in.** Apagar linha devolve espaço à free list do SQLite, não ao disco;
  só o VACUUM encolhe o arquivo, reescrevendo o banco inteiro. Roda **semanalmente**
  (domingo) no agendado, e o backend remoto do Turso pode recusá-lo — quando recusa, o
  job avisa e segue verde: as linhas já saíram.
- **`--dry-run` não exporta métricas**, de propósito: simulação não deve mexer nas séries
  que os alertas observam.

## 3. O job agendado

[`retention.yml`](../.github/workflows/retention.yml) roda **todo dia às 04:17 UTC**
(~01:17 BRT) e aceita `workflow_dispatch` com `dry_run`/`vacuum`.

Fica no GitHub Actions, e não num cron da EC2 do Hermes, porque: a EC2 ainda não tem
deploy do ops-centro (fase 3), os secrets do Turso já estão no Actions, e a run deixa log
e histórico auditáveis sem SSH. Migrar para o Hermes depois é trocar o executor — o
comando é o mesmo.

Cada run sobe um artefato `retention-report-<run_id>.json` (90 dias) com o relatório
completo: é o "log de execução" do critério de aceite da issue.

Sem `TURSO_DATABASE_URL` nos secrets o job sai **verde sem fazer nada** — repo clonado ou
fork não deve receber CI vermelho por não ter banco provisionado.

## 4. O observador também é observado

O job publica as próprias métricas por OTLP (catálogo em
[`ops_centro/metrics.py`](../ops_centro/metrics.py)):

| Métrica | Tipo | Para quê |
| --- | --- | --- |
| `ops_centro_log_retention_deleted_total{level}` | counter | quanto saiu, por nível |
| `ops_centro_log_retention_duration_seconds` | histogram | duração da passada — e prova de que rodou |
| `ops_centro_logs_rows` | gauge | linhas restantes |
| `ops_centro_logs_db_bytes` | gauge | tamanho do banco — base do alerta de teto |

`ops_centro_logs_db_bytes` sai de `page_count * page_size` (medida barata, não varre
linha). Backend que recuse os pragmas devolve "desconhecido" em vez de zero — zero seria
uma mentira que o alerta acreditaria.

## 5. Alertas

[`grafana/alerts/turso-retencao.yaml`](../grafana/alerts/turso-retencao.yaml), formato de
provisionamento do Grafana Alerting. Três regras:

| uid | Dispara quando | Severidade |
| --- | --- | --- |
| `ops-centro-turso-db-70` | banco > 70% dos 9 GiB, por 30min | warning |
| `ops-centro-turso-db-90` | banco > 90% dos 9 GiB, por 15min | critical |
| `ops-centro-retencao-parada` | nenhuma execução do job em 36h | warning |

A terceira é a que vigia as outras duas: job parado deixa `ops_centro_logs_db_bytes`
congelado, e um alerta olhando para série congelada nunca dispara.

Importar (Grafana Cloud): *Alerting → Alert rules → Import*, substituindo `${DS_PROM}`
pelo uid do datasource Prometheus (`grafanacloud-prom`). O roteamento para o
Hermes/Telegram é a issue #12; até lá, as regras usam o contact point default.

## 6. Quando o alerta de 70% disparar

Na ordem:

1. **O job rodou?** `ops_centro_log_retention_deleted_total` teve incremento nas últimas
   24h? Se não, o problema é o workflow, não a política.
2. **Encurtar a janela** via `TURSO_LOG_RETENTION_DAYS` — comece por `INFO`, que costuma
   ser 90% das linhas. Confirme com `make retention-dry` antes.
3. **`make retention-vacuum` uma vez**, para devolver ao disco o espaço já liberado.
4. **Cortar na origem:** se o volume vem de um app mandando `INFO` para o Turso, o
   conserto é no app ([turso-logs.md](turso-logs.md#o-que-logar)), não aqui.
