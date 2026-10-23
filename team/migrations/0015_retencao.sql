-- 0015: exclusao controlada de demandas. Os gatilhos "somente de acrescimo" passam a aceitar DELETE apenas
-- dentro de uma transacao que registrou a guarda de exclusao (erasure_guard); UPDATE continua proibido.
CREATE TABLE erasure_guard (token INTEGER PRIMARY KEY CHECK (token = 1));
CREATE TABLE erasure_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  case_id TEXT NOT NULL,
  erased_at TEXT NOT NULL,
  erased_by TEXT NOT NULL,
  counts TEXT NOT NULL CHECK (json_valid(counts))
);
CREATE TRIGGER erasure_log_append_only_update BEFORE UPDATE ON erasure_log
BEGIN SELECT RAISE(ABORT, 'o registro de exclusoes e somente de acrescimo'); END;
CREATE TRIGGER erasure_log_append_only_delete BEFORE DELETE ON erasure_log
BEGIN SELECT RAISE(ABORT, 'o registro de exclusoes e somente de acrescimo'); END;
DROP TRIGGER case_entries_append_only_delete;
CREATE TRIGGER case_entries_append_only_delete BEFORE DELETE ON case_entries
WHEN NOT EXISTS (SELECT 1 FROM erasure_guard)
BEGIN SELECT RAISE(ABORT, 'entradas da demanda sao somente de acrescimo'); END;
DROP TRIGGER evidences_append_only_delete;
CREATE TRIGGER evidences_append_only_delete BEFORE DELETE ON evidences
WHEN NOT EXISTS (SELECT 1 FROM erasure_guard)
BEGIN SELECT RAISE(ABORT, 'evidencias sao somente de acrescimo; registre uma nova'); END;
DROP TRIGGER triage_decisions_append_only_delete;
CREATE TRIGGER triage_decisions_append_only_delete BEFORE DELETE ON triage_decisions
WHEN NOT EXISTS (SELECT 1 FROM erasure_guard)
BEGIN SELECT RAISE(ABORT, 'decisoes de triagem sao somente de acrescimo'); END;
