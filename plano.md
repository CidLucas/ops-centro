# Plano: Serviço de Observabilidade Centralizada

## 1. Objetivo

Criar um sistema centralizado de observabilidade, de custo zero ou próximo de zero, capaz de coletar métricas, logs e traces dos dois produtos atuais (plataforma de agentes/MCP e serviço de memória de arquivos), exibi-los em dashboards unificados, gerar alertas inteligentes e permitir ações de monitoramento e resposta via Hermes (Telegram).

## 2. Escopo

**Incluído:**

- Instrumentação OpenTelemetry nos dois aplicativos existentes
- Pipeline de coleta e armazenamento de telemetria em free tier
- Dashboards unificados por aplicação, cliente e ambiente
- Sistema de alertas com webhook para o Hermes
- Hermes como canal de notificação enriquecida e executor de ações (com confirmação para ações destrutivas)

**Fora de escopo (fase inicial):**

- Detecção de anomalias com ML
- Circuit breaker totalmente autônomo sem supervisão
- Multi-região / alta disponibilidade do pipeline de observabilidade

## 3. Arquitetura Geral

```
┌─────────────────────┐     ┌──────────────────────┐
│  Agents Platform     │     │  File Memory / MCP    │
│  (agentes + MCP)     │     │  Service              │
└──────────┬───────────┘     └───────────┬───────────┘
           │ OTLP (métricas, traces, logs)│
           └───────────────┬──────────────┘
                            ▼
                 ┌─────────────────────┐
                 │   Grafana Cloud      │
                 │ (free tier: Prom +   │
                 │  Loki + Tempo)       │
                 └──────────┬───────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        Dashboards      Alertas       Logs longos
        (Grafana)      (webhook)      → Turso DB
                            │
                            ▼
                 ┌─────────────────────┐
                 │  Hermes (EC2)        │
                 │  - recebe webhook    │
                 │  - consulta Turso    │
                 │  - notifica Telegram │
                 │  - executa ações     │
                 └─────────────────────┘
```

## 4. Requisitos Funcionais

| ID   | Requisito                                                                                                      |
| ---- | -------------------------------------------------------------------------------------------------------------- |
| RF01 | Cada aplicativo deve emitir métricas, traces e logs via OTLP para o Grafana Cloud                              |
| RF02 | Todo sinal emitido deve carregar `app_name`, `environment`, `tenant_id`, `version`, `timestamp`                |
| RF03 | Traces de execução de agentes devem registrar tool calls como spans filhos                                     |
| RF04 | Eventos de ingestão de arquivos devem registrar cada etapa do pipeline (upload → processamento → MCP pronto)   |
| RF05 | Logs estruturados detalhados devem ser persistidos no Turso, correlacionados por `trace_id`                    |
| RF06 | Grafana deve disparar alertas via webhook quando métricas ultrapassarem limiares definidos                     |
| RF07 | Hermes deve receber o webhook, consultar contexto adicional no Turso e enviar mensagem enriquecida no Telegram |
| RF08 | Hermes deve permitir consultas sob demanda ("como estão os agentes hoje?")                                     |
| RF09 | Hermes deve poder executar ações de baixo risco automaticamente (ex: pausar uma tool com falha recorrente)     |
| RF10 | Hermes deve pedir confirmação antes de ações de maior impacto (ex: reiniciar serviço)                          |

## 5. Requisitos Não Funcionais

| ID    | Requisito                                                                                                           |
| ----- | ------------------------------------------------------------------------------------------------------------------- |
| RNF01 | Custo mensal alvo: R$ 0 a R$ 50 na fase de validação                                                                |
| RNF02 | Sampling de traces configurado para não estourar o free tier do Grafana Cloud (ex: 100% em erro, 5-10% em sucesso)  |
| RNF03 | Retenção mínima de 14 dias no Grafana Cloud (padrão do free tier)                                                   |
| RNF04 | Latência de emissão de telemetria não deve impactar performance perceptível dos apps (uso assíncrono/batch)         |
| RNF05 | Schema de dados comum entre os dois aplicativos, para permitir queries e dashboards cruzados                        |
| RNF06 | Segurança: API keys do Grafana e tokens do Telegram armazenados como variáveis de ambiente/secrets, nunca em código |

## 6. Schema de Dados (resumo)

**Atributos comuns:** `app_name`, `environment`, `tenant_id`, `version`, `timestamp`

**Agents Platform:**

- `agent_execution` (trace pai): status, duração, tokens, custo, modelo LLM
- `mcp_tool_call` (span filho): tool_name, mcp_server, status, duração, retries, erro

**File Memory / MCP:**

- `file_ingestion`: file_id, tipo, tamanho, status por etapa, duração
- `mcp_memory_query`: mcp_server_id, tipo de query, duração, status, resultado

**Logs (Turso):**

```sql
CREATE TABLE logs (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  app_name TEXT,
  tenant_id TEXT,
  trace_id TEXT,
  level TEXT,
  message TEXT,
  metadata TEXT
);
```

## 7. Métricas Prioritárias (fase inicial)

- Taxa de erro por agente / por tool
- Latência p50/p95/p99 de chamadas de MCP tools e LLM
- Taxa de falha na ingestão de arquivos
- Latência de queries MCP no serviço de memória
- Volume de uso por tenant (execuções, arquivos processados)

## 8. Ações do Hermes

**Autônomas (baixo risco):**

- Pausar temporariamente uma tool MCP com falhas recorrentes
- Enviar notificação enriquecida com contexto do erro

**Com confirmação (maior risco):**

- Reiniciar serviços via SSH/API na AWS
- Reverter uma pausa de tool

**Sob demanda:**

- Responder perguntas sobre status dos sistemas consultando Grafana API / Turso

## 9. Fases de Implementação

### Fase 1 — Fundação (semana 1-2)

- Criar conta Grafana Cloud (free tier)
- Definir schema de dados e convenções de nomenclatura
- Instrumentar um app piloto (recomendado: Agents Platform) com OpenTelemetry SDK
- Validar chegada de métricas/traces no Grafana

### Fase 2 — Expansão (semana 3-4)

- Instrumentar o segundo app (File Memory / MCP)
- Criar tabela de logs no Turso e implementar gravação correlacionada por `trace_id`
- Construir dashboards iniciais no Grafana (por app, por tenant)

### Fase 3 — Alertas e Hermes (semana 5-6)

- Configurar contact points/webhooks no Grafana
- Implementar endpoint no Hermes (EC2) para receber alertas
- Implementar lógica de enriquecimento de mensagem (consulta ao Turso)
- Integrar envio para Telegram

### Fase 4 — Ações automatizadas (semana 7+)

- Implementar ações de baixo risco (pausar tool)
- Implementar fluxo de confirmação para ações de maior risco
- Implementar consultas sob demanda via Telegram

## 10. Riscos e Mitigações

| Risco                                                    | Mitigação                                                               |
| -------------------------------------------------------- | ----------------------------------------------------------------------- |
| Estourar free tier do Grafana Cloud com volume de traces | Sampling agressivo desde o início; monitorar uso do próprio free tier   |
| Ação automatizada do Hermes causar impacto indevido      | Exigir confirmação para ações destrutivas; logar todas as ações tomadas |
| Falta de padronização entre os dois apps                 | Definir schema comum antes de instrumentar o segundo app                |
| Custo do Turso crescer com volume de logs                | Definir política de retenção/limpeza de logs antigos                    |

## 11. Estimativa de Custo

| Item                      | Custo estimado                   |
| ------------------------- | -------------------------------- |
| Grafana Cloud (free tier) | R$ 0                             |
| Turso (free tier)         | R$ 0                             |
| EC2 do Hermes             | já existente (custo já assumido) |
| Telegram Bot API          | R$ 0                             |
| **Total estimado**        | **R$ 0 (fase de validação)**     |

## 12. Critérios de Sucesso

- Ambos os apps emitindo telemetria consistente para o Grafana Cloud
- Dashboards permitindo visualizar saúde de cada app e de cada tenant
- Alertas chegando no Telegram via Hermes com contexto útil (não apenas "algo deu errado")
- Pelo menos uma ação automatizada de baixo risco funcionando de ponta a ponta
