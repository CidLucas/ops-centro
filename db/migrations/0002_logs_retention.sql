-- Índice de suporte à política de retenção (issue #9).
-- O job de limpeza apaga por (nível, janela de tempo): sem este índice o DELETE vira
-- full scan da tabela, o que no free tier do Turso custa row reads a cada passada —
-- exatamente o custo que a retenção existe para conter.
CREATE INDEX IF NOT EXISTS idx_logs_level_time ON logs (level, timestamp);
