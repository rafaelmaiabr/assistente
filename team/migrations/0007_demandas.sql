-- 0007: linha do tempo da demanda (notas, mensagens, resumos) e evidencias, ambas somente de acrescimo.
CREATE TABLE case_entries (
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
CREATE INDEX case_entries_by_case ON case_entries (tenant_id, case_id, created_at, id);
CREATE TRIGGER case_entries_append_only_update BEFORE UPDATE ON case_entries
BEGIN SELECT RAISE(ABORT, 'entradas da demanda sao somente de acrescimo'); END;
CREATE TRIGGER case_entries_append_only_delete BEFORE DELETE ON case_entries
BEGIN SELECT RAISE(ABORT, 'entradas da demanda sao somente de acrescimo'); END;
CREATE TRIGGER evidences_append_only_update BEFORE UPDATE ON evidences
BEGIN SELECT RAISE(ABORT, 'evidencias sao somente de acrescimo; registre uma nova'); END;
CREATE TRIGGER evidences_append_only_delete BEFORE DELETE ON evidences
BEGIN SELECT RAISE(ABORT, 'evidencias sao somente de acrescimo; registre uma nova'); END;
