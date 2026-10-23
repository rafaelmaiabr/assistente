-- 0010: catalogo de capacidades por empresa e decisoes de triagem (somente de acrescimo).
CREATE TABLE capabilities (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
CREATE TABLE triage_decisions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE INDEX triage_by_case ON triage_decisions (tenant_id, case_id, created_at, id);
CREATE TRIGGER triage_decisions_append_only_update BEFORE UPDATE ON triage_decisions
BEGIN SELECT RAISE(ABORT, 'decisoes de triagem sao somente de acrescimo'); END;
CREATE TRIGGER triage_decisions_append_only_delete BEFORE DELETE ON triage_decisions
BEGIN SELECT RAISE(ABORT, 'decisoes de triagem sao somente de acrescimo'); END;
