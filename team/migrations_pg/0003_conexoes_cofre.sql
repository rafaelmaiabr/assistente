-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0003_conexoes_cofre.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0003: credenciais (somente metadados; o valor fica no cofre) e catalogo MCP aprovado.
CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE TABLE mcp_catalog (
  id TEXT PRIMARY KEY,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  catalog_key TEXT NOT NULL,
  entry_version TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (catalog_key, entry_version)
);
ALTER TABLE connections ADD COLUMN credential_ref TEXT REFERENCES credentials(id);
ALTER TABLE connections ADD COLUMN catalog_entry_id TEXT REFERENCES mcp_catalog(id);
CREATE FUNCTION connections_credential_tenant_insert_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.credential_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM credentials WHERE id = NEW.credential_ref AND tenant_id = NEW.tenant_id) THEN RAISE EXCEPTION 'credencial de outra empresa' USING ERRCODE = '23000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER connections_credential_tenant_insert BEFORE INSERT ON connections FOR EACH ROW EXECUTE FUNCTION connections_credential_tenant_insert_fn();
CREATE FUNCTION connections_credential_tenant_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.credential_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM credentials WHERE id = NEW.credential_ref AND tenant_id = NEW.tenant_id) THEN RAISE EXCEPTION 'credencial de outra empresa' USING ERRCODE = '23000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER connections_credential_tenant_update BEFORE UPDATE OF credential_ref, tenant_id ON connections FOR EACH ROW EXECUTE FUNCTION connections_credential_tenant_update_fn();
