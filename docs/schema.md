# Schema Comum de Telemetria — v1 (congelado)

> **Status:** congelado em 2026-07-23 (issue [#3](https://github.com/CidLucas/ops-centro/issues/3)).
> **Fonte de verdade executável:** [`ops_centro/conventions.py`](../ops_centro/conventions.py).
> Qualquer mudança passa por PR neste repo **antes** de tocar os consumidores (RNF05, §10 do plano).

Repos consumidores:

| App (`app_name`)  | Repo            | Sinais                                                   |
| ----------------- | --------------- | -------------------------------------------------------- |
| `agents-platform` | `repo_platform` | spans `agent_execution`, `mcp_tool_call`; métricas `agents_platform_*` |
| `file-memory-mcp` | `mcp_brain`     | spans `file_ingestion`, `mcp_memory_query`; métricas `context_mcp_*` |
| `ops-centro`      | este repo       | receiver de alertas; métricas `ops_centro_*`              |

---

## 1. Atributos comuns (RF02)

Todo sinal (trace, métrica, log) carrega:

| Atributo      | Onde                                   | Valores                                  |
| ------------- | -------------------------------------- | ---------------------------------------- |
| `app_name`    | Resource                               | `agents-platform` \| `file-memory-mcp` \| `ops-centro` |
| `environment` | Resource                               | `dev` \| `staging` \| `prod`             |
| `version`     | Resource                               | semver do app em deploy                  |
| `tenant_id`   | **por request/span** (não no Resource) | id do cliente; ausente em sinais sem tenant |
| `timestamp`   | implícito no protocolo OTLP            | —                                        |

`build_resource_attributes()` em `conventions.py` monta o dicionário e **rejeita** valores fora
do vocabulário canônico (fail-fast contra divergência silenciosa).

Montagem: os apps passam o dicionário para `setup_observability(resource_attributes=...)` da
`blu_observability_bootstrap` ≥ 0.3.0 (issue [#4](https://github.com/CidLucas/ops-centro/issues/4)).

**Validação no Grafana (critério de aceite do #3):** no Tempo,
`{ resource.app_name != nil && resource.environment != nil && resource.version != nil }`
deve retornar 100% dos traces de cada app.

## 2. Spans canônicos (§6 do plano)

Nomes de span e de atributo vêm de `conventions.py` — **sem strings soltas** nos apps.
Status de erro usa o mecanismo nativo do OTel (`span.status = ERROR` + `record_exception`);
duração é implícita do span.

### `agent_execution` (agents-platform) — trace pai, 1 por execução de agente

| Atributo        | Tipo   | Nota                                    |
| --------------- | ------ | --------------------------------------- |
| `agent_name`    | string | ex.: `frontdesk`, `financeiro`          |
| `model`         | string | modelo LLM resolvido                    |
| `tokens_input`  | int    | soma da execução (quando disponível)    |
| `tokens_output` | int    | idem                                    |
| `cost_usd`      | double | emitido só quando calculável            |
| `session_id`    | string | correlação com Langfuse                 |
| `tenant_id`     | string | obrigatório por request                 |

### `mcp_tool_call` (agents-platform) — filho de `agent_execution`, 1 por tool call

| Atributo     | Tipo   | Nota                                       |
| ------------ | ------ | ------------------------------------------ |
| `tool_name`  | string | nome da tool MCP                           |
| `mcp_server` | string | URL/id do servidor MCP                     |
| `retries`    | int    | emitido quando observável                  |
| `tenant_id`  | string | propagado do request                       |

### `file_ingestion` (file-memory-mcp) — 1 por arquivo processado

| Atributo          | Tipo   | Nota                              |
| ----------------- | ------ | --------------------------------- |
| `file_id`         | string | id interno (nunca em métrica)     |
| `file_type`       | string | mime/extensão                     |
| `file_size_bytes` | int    |                                   |
| `stage`           | string | parse/embed/graph/extract/...     |
| `tenant_id`       | string |                                   |

### `mcp_memory_query` (file-memory-mcp) — 1 por query MCP

| Atributo        | Tipo   | Nota                    |
| --------------- | ------ | ----------------------- |
| `mcp_server_id` | string |                         |
| `query_type`    | string | tipo de query           |
| `result_count`  | int    |                         |
| `tenant_id`     | string |                         |

## 3. Convenção de métricas

- **Prefixo por app** (snake_case): `agents_platform_`, `context_mcp_`, `ops_centro_`.
  `context_mcp` já está em produção no mcp_brain e fica congelado como está.
- **Sufixos/unidades:** counters `_total`; histogramas de latência `_duration_seconds`
  (unit OTel `"s"`, valores em segundos); tamanhos `_bytes`. Nada de `_ms`.
- **Labels permitidas** (cardinalidade baixa, enumerável): `app_name`, `environment`,
  `tool`, `stage`, `status`, `agent`, `model`. `tenant_id` só em métricas de volume/uso
  (§7 do plano). **Proibido:** `session_id`, `trace_id`, `file_id`, texto livre.
- `status` em métricas usa o vocabulário `ok` | `error` (já adotado pelo `context_mcp_*`).

Métricas prioritárias (§7) derivam dos spans acima ou destes instrumentos — antes de criar
uma métrica nova, verifique se uma query no Tempo sobre os spans não resolve.

## 4. Mecanismo de compartilhamento (decisão)

**Constantes replicadas nos consumidores + teste de paridade congelado.**

Motivo: importar `ops-centro` como dependência git arrastaria as dependências do receiver
(fastapi, libsql, ...) para dentro dos apps e criaria dependência git circular com o
`repo_platform` (o ops-centro já consome `blu_observability_bootstrap` de lá). O vocabulário
é pequeno e **congelado** — replicar é barato e o risco de deriva é coberto por teste.

Regras para cada consumidor:

1. Replicar apenas as constantes que usa (nomes de span, atributos, prefixo de métrica),
   em um único módulo, com comentário apontando para este arquivo.
2. Adicionar um teste unitário que pina os **valores literais** (não só compara símbolos) —
   ex.: `assert SPAN_AGENT_EXECUTION == "agent_execution"`. Este repo mantém o mesmo teste
   em [`tests/test_conventions.py`](../tests/test_conventions.py); os dois só podem mudar juntos.
3. Mudança de schema = PR aqui primeiro (bump da versão deste doc) + issues nos consumidores.

## 5. Sampling (RNF02)

- Ratio de sucesso configurável por env `OTEL_TRACES_SAMPLER_ARG` (alvo: 0.05–0.10 em prod,
  1.0 em dev) — implementado na `blu_observability_bootstrap` ≥ 0.3.0.
- **Erros são sempre exportados** (tail-based simplificado: filtro no processor de export,
  não no sampler). Caveat documentado: um trace de erro não sorteado pelo ratio pode chegar
  incompleto (spans de sucesso do mesmo trace descartados) — aceitável para o free tier.
