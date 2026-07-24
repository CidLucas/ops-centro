-- Auditoria das ações do Hermes (mitigação §10 do plano; base da issue #19).
--
-- A issue #17 (pausa autônoma de tool) é a primeira escritora, e a #18 (ações com
-- confirmação) usa a mesma tabela: toda proposta, execução e falha vira uma linha. Sem
-- isso, "ação automatizada causar impacto indevido" (§10) vira investigação sem trilha.
--
-- Duas colunas fazem o trabalho pesado de reconstrução:
--   trace_id   — liga a ação ao alerta que a motivou e aos logs daquela execução (RF05);
--   expires_at — quando a pausa deve terminar. É por ela que o despausar automático
--                sobrevive a um restart do receiver: o estado mora aqui, não na memória.
--
-- Retenção longa de propósito (fora da limpeza da issue #9): auditoria antiga ainda
-- responde "quem pausou essa tool em julho?".
CREATE TABLE IF NOT EXISTS action_audit (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,          -- ops-centro (autônoma) | telegram:<user> (confirmada, #18)
  action TEXT NOT NULL,         -- pause_tool | resume_tool
  target TEXT NOT NULL,         -- nome da tool
  app_name TEXT,
  tenant_id TEXT,
  trace_id TEXT,
  triggered_by TEXT,            -- o que motivou (alertname + contagem de falhas)
  reason TEXT,                  -- texto humano, o mesmo que vai no Telegram
  ttl_seconds INTEGER,
  expires_at TEXT,              -- NULL para ações sem TTL (ex: resume_tool)
  status TEXT NOT NULL,         -- ok | error | bloqueado (kill switch, cooldown, sem config)
  detail TEXT
);

-- "Esta tool está pausada agora?" e "o que venceu e precisa ser despausado?" — as duas
-- perguntas do laço de TTL, ambas por (target, expires_at).
CREATE INDEX IF NOT EXISTS idx_action_audit_target ON action_audit (target, expires_at);
-- Histórico recente para o `/acoes` (#19) e para o relatório de status (#16).
CREATE INDEX IF NOT EXISTS idx_action_audit_time ON action_audit (created_at);
