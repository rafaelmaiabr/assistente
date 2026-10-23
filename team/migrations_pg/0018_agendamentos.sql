-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0018_agendamentos.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0018: rotinas agendadas (opt-in), notificacoes ao operador e escalonamentos de SLA ja realizados.
CREATE TABLE schedules (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  next_run_at TEXT NOT NULL,
  job_kind TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX schedules_due ON schedules (state, next_run_at);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  severity TEXT NOT NULL,
  topic TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX notifications_by_key ON notifications (tenant_id, dedupe_key, created_at);
CREATE TABLE sla_escalations (
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  level BIGINT NOT NULL CHECK (level >= 1),
  escalated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, subject_id, level)
);
