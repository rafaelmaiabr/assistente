-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0010_triagem.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0010: catalogo de capacidades por empresa e decisoes de triagem (somente de acrescimo).
CREATE TABLE capabilities (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
CREATE TABLE triage_decisions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX triage_by_case ON triage_decisions (tenant_id, case_id, created_at, id);
CREATE FUNCTION triage_decisions_append_only_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'decisoes de triagem sao somente de acrescimo' USING ERRCODE = '23000'; RETURN NEW; END $$;
CREATE TRIGGER triage_decisions_append_only_update BEFORE UPDATE ON triage_decisions FOR EACH ROW EXECUTE FUNCTION triage_decisions_append_only_update_fn();
CREATE FUNCTION triage_decisions_append_only_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'decisoes de triagem sao somente de acrescimo' USING ERRCODE = '23000'; RETURN OLD; END $$;
CREATE TRIGGER triage_decisions_append_only_delete BEFORE DELETE ON triage_decisions FOR EACH ROW EXECUTE FUNCTION triage_decisions_append_only_delete_fn();
