-- 0018: rotinas agendadas (opt-in), notificacoes ao operador e escalonamentos de SLA ja realizados.
CREATE TABLE schedules (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  next_run_at TEXT NOT NULL,
  job_kind TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE INDEX schedules_due ON schedules (state, next_run_at);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  severity TEXT NOT NULL,
  topic TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE INDEX notifications_by_key ON notifications (tenant_id, dedupe_key, created_at);
CREATE TABLE sla_escalations (
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  level INTEGER NOT NULL CHECK (level >= 1),
  escalated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, subject_id, level)
);
