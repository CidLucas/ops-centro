-- Dead-letter das notificações que não chegaram ao Hermes (RF07 — parte 2, issue #15).
--
-- Critério de aceite do #15: "queda do Hermes não perde alerta silenciosamente". Depois de
-- esgotar as tentativas com backoff, o receiver grava aqui o envelope inteiro — é o que
-- permite reenviar à mão e, principalmente, saber que houve alerta que ninguém viu.
--
-- Volume esperado é baixo por construção (só o que falhou), então esta tabela fica fora da
-- limpeza agressiva da issue #9: perder a prova de um alerta não entregue custa mais que
-- alguns KB no free tier.
CREATE TABLE IF NOT EXISTS hermes_dead_letter (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,          -- alert | action (contrato em ops_centro/receiver/hermes.py)
  group_key TEXT,              -- mesma chave de agrupamento da notificação
  severity TEXT,
  app_name TEXT,
  tenant_id TEXT,
  trace_id TEXT,
  attempts INTEGER NOT NULL,   -- tentativas gastas antes de desistir
  reason TEXT NOT NULL,        -- por que desistiu (HTTP 5xx, timeout, rede...)
  payload TEXT NOT NULL,       -- envelope JSON completo, pronto para reenvio
  resent_at TEXT               -- preenchido à mão quando alguém reenvia
);

-- Consulta natural: "o que ficou pendente hoje?" (varredura por tempo, do mais recente).
CREATE INDEX IF NOT EXISTS idx_hermes_dead_letter_time ON hermes_dead_letter (created_at);
-- Pendências ainda não reenviadas — o que um comando de status precisa contar barato.
CREATE INDEX IF NOT EXISTS idx_hermes_dead_letter_pendente ON hermes_dead_letter (resent_at, created_at);
