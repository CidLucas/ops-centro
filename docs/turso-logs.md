# Logs de longa retenção no Turso (RF05)

> Issue [#8](https://github.com/CidLucas/ops-centro/issues/8). Código:
> [`ops_centro/turso/`](../ops_centro/turso/) · migration:
> [`db/migrations/0001_create_logs.sql`](../db/migrations/0001_create_logs.sql).

O Grafana Cloud free tier retém logs por ~14 dias (RNF03). O que precisa sobreviver a
isso — e alimentar o enriquecimento de alertas do Hermes (RF07) e as consultas sob
demanda (RF08) — vai para uma tabela `logs` no Turso, correlacionada com os traces do
Tempo pelo `trace_id`.

## 1. Provisionar o banco (passo manual, uma vez)

Requer a [CLI do Turso](https://docs.turso.tech/cli/installation) autenticada
(`turso auth login`):

```bash
turso db create ops-centro-logs --group default
turso db show ops-centro-logs --url          # → TURSO_DATABASE_URL
turso db tokens create ops-centro-logs       # → TURSO_AUTH_TOKEN
```

Grave os dois valores no `.env` local, nos secrets dos repos consumidores e no `.env`
da EC2 do Hermes (mapa em [secrets.md](secrets.md)). Nenhum valor entra em código (RNF06).

## 2. Aplicar a migration

```bash
make migrate            # aplica em TURSO_DATABASE_URL
make migrate-status     # lista o que já foi aplicado
```

O aplicador (`ops_centro.turso.migrate`) registra cada migration em `schema_migrations`
com checksum: reaplicar é no-op e editar uma migration já aplicada gera aviso — mudança
de schema entra como **nova** migration.

## 3. Usar o writer nos apps

```python
from ops_centro.turso import log_to_turso

log_to_turso(
    "agents-platform",          # app_name (vocabulário de conventions.py)
    tenant_id,                  # opcional
    None,                       # trace_id: None = pega do span OTel ativo
    "ERROR",
    "tool de busca falhou após 3 retries",
    {"tool": "search", "retries": 3},
)
```

Garantias:

- **Não bloqueia o caminho quente (RNF04).** A chamada só serializa a metadata e
  enfileira; uma thread daemon grava em batch (50 registros ou 2s, configurável por
  `TURSO_LOG_BATCH_SIZE` / `TURSO_LOG_FLUSH_INTERVAL`). Coberto por teste
  (`test_log_nao_bloqueia_no_caminho_quente`).
- **Fila cheia descarta** (10k registros) e conta em `stats()["dropped"]` — log perdido
  é melhor que request travado.
- **`trace_id` automático** do span ativo, em 32 hex, o mesmo formato exibido no Tempo.
- **Sem `TURSO_DATABASE_URL` vira no-op** silencioso: CI e dev sem banco seguem verdes.
- Falha de escrita = uma reconexão + uma retentativa; persistindo, o batch é descartado
  (a fila não cresce sem limite).

Encerramento: chame `shutdown_log_writer()` no shutdown do processo para drenar o que
está na fila (no receiver isso já acontece no `lifespan`).

### O que logar

Só o que justifica retenção longa: `ERROR`/`WARNING` e eventos de negócio relevantes
(ingestão concluída, execução de agente finalizada). Volume alto de `INFO` é papel do
Loki, com retenção curta — mandar tudo para o Turso queima o free tier de escrita.

### Consumo a partir dos apps (decisão)

Igual ao schema comum ([schema.md §4](schema.md#4-mecanismo-de-compartilhamento-decisão)):
os apps **não** importam `ops-centro` como dependência (arrastaria fastapi/uvicorn e
criaria dependência git circular com o `repo_platform`). Cada app copia
[`log_writer.py`](../ops_centro/turso/log_writer.py) + [`connection.py`](../ops_centro/turso/connection.py)
para o seu módulo de observabilidade, com comentário apontando para cá; os dois arquivos
dependem só de `libsql` e da stdlib, de propósito.

**Atenção às envs no consumidor:** app que já usa Turso para o próprio produto (é o caso
do mcp_brain) não pode reaproveitar `TURSO_DATABASE_URL` — são bancos diferentes. A cópia
de lá usa `OPS_LOGS_DATABASE_URL` / `OPS_LOGS_AUTH_TOKEN`.

Estado da adoção:

| App | Módulo | O que grava |
| --- | --- | --- |
| `file-memory-mcp` (mcp_brain) | `common/log_export.py` | desfecho de cada job de ingestão (`done`/`error`/`dead_letter`/`retry`), com o `trace_id` do span `file_ingestion` |
| `agents-platform` (repo_platform) | ⏳ pendente | depende da instrumentação de traces do [#5](https://github.com/CidLucas/ops-centro/issues/5) |
| `ops-centro` | `ops_centro/turso/` | writer nativo; o receiver drena a fila no shutdown |

## 4. Retenção e custo

O free tier do Turso dá ~9 GB de storage e 1 bilhão de row reads/mês. A política de
limpeza (janela de retenção + job de purge) é a issue
[#9](https://github.com/CidLucas/ops-centro/issues/9) — até ela existir, monitore o
tamanho com `turso db inspect ops-centro-logs`.
