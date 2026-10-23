-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0007_demandas.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0007: linha do tempo da demanda (notas, mensagens, resumos) e evidencias, ambas somente de acrescimo.
CREATE TABLE case_entries (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE INDEX case_entries_by_case ON case_entries (tenant_id, case_id, created_at, id);
CREATE FUNCTION case_entries_append_only_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'entradas da demanda sao somente de acrescimo' USING ERRCODE = '23000'; RETURN NEW; END $$;
CREATE TRIGGER case_entries_append_only_update BEFORE UPDATE ON case_entries FOR EACH ROW EXECUTE FUNCTION case_entries_append_only_update_fn();
CREATE FUNCTION case_entries_append_only_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'entradas da demanda sao somente de acrescimo' USING ERRCODE = '23000'; RETURN OLD; END $$;
CREATE TRIGGER case_entries_append_only_delete BEFORE DELETE ON case_entries FOR EACH ROW EXECUTE FUNCTION case_entries_append_only_delete_fn();
CREATE FUNCTION evidences_append_only_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'evidencias sao somente de acrescimo; registre uma nova' USING ERRCODE = '23000'; RETURN NEW; END $$;
CREATE TRIGGER evidences_append_only_update BEFORE UPDATE ON evidences FOR EACH ROW EXECUTE FUNCTION evidences_append_only_update_fn();
CREATE FUNCTION evidences_append_only_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'evidencias sao somente de acrescimo; registre uma nova' USING ERRCODE = '23000'; RETURN OLD; END $$;
CREATE TRIGGER evidences_append_only_delete BEFORE DELETE ON evidences FOR EACH ROW EXECUTE FUNCTION evidences_append_only_delete_fn();
