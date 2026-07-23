-- Tabela de logs estruturados de longa retenção (RF05, seção 6 do plano).
-- Escrita pelos apps (agents-platform e file-memory-mcp), correlacionada por
-- trace_id com os traces do Grafana Tempo. Lida pelo receiver/Hermes para
-- enriquecer alertas (RF07) e responder consultas sob demanda (RF08).
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  app_name TEXT NOT NULL,
  tenant_id TEXT,
  trace_id TEXT,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata TEXT
);

-- Correlação por trace_id é o caminho de leitura principal do enriquecimento.
CREATE INDEX IF NOT EXISTS idx_logs_trace_id ON logs (trace_id);
-- Consultas por app/tenant em janelas de tempo (dashboards, RF08).
CREATE INDEX IF NOT EXISTS idx_logs_app_time ON logs (app_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_tenant_time ON logs (tenant_id, timestamp);
