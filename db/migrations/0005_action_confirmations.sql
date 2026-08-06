-- Tokens de confirmação das ações de maior impacto (RF10, issue #18).
--
-- A trilha de auditoria (`action_audit`, migration 0004) responde "o que foi feito".
-- Esta tabela responde a pergunta anterior: "isto aqui ainda pode ser executado?".
--
-- Três propriedades que a coluna sozinha não dá, e que são o motivo de a tabela existir:
--   token_hash — só o SHA-256 do token é guardado. Quem lê o banco (backup, dump, log de
--                query) não consegue confirmar uma ação em nome de ninguém;
--   status     — a transição `pendente` → `confirmado` é feita por UPDATE condicional, o
--                que torna o token de **uso único** mesmo se o Telegram reenviar o mesmo
--                callback duas vezes (acontece com rede ruim no celular);
--   expires_at — confirmação velha não executa. Um "sim" de ontem não autoriza um restart
--                hoje, quando o incidente já é outro.
--
-- `chat_id` fica gravado com a proposta: só o mesmo chat que pediu pode confirmar, o que
-- impede que um token vazado seja usado de outra conversa.
CREATE TABLE IF NOT EXISTS action_confirmations (
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  action TEXT NOT NULL,          -- restart_service | resume_tool (allowlist de confirmations.py)
  target TEXT NOT NULL,          -- app a reiniciar ou tool a despausar
  app_name TEXT,
  tenant_id TEXT,
  trace_id TEXT,
  chat_id TEXT NOT NULL,         -- chat do Telegram que propôs (e o único que pode confirmar)
  requested_by TEXT NOT NULL,    -- telegram:<user>
  reason TEXT,                   -- descrição do impacto, a mesma que foi ao Telegram
  status TEXT NOT NULL,          -- pendente | confirmado | cancelado | expirado
  decided_at TEXT,
  decided_by TEXT,
  audit_id INTEGER               -- linha da proposta em action_audit (#19)
);

-- "O que ainda está pendente e não venceu?" — a varredura do `--pending` e a única
-- consulta que não é por token.
CREATE INDEX IF NOT EXISTS idx_action_confirmations_status
  ON action_confirmations (status, expires_at);
