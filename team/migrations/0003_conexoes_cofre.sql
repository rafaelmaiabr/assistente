-- 0003: credenciais (somente metadados; o valor fica no cofre) e catalogo MCP aprovado.
CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE TABLE mcp_catalog (
  id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  catalog_key TEXT NOT NULL,
  entry_version TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (catalog_key, entry_version)
);
ALTER TABLE connections ADD COLUMN credential_ref TEXT REFERENCES credentials(id);
ALTER TABLE connections ADD COLUMN catalog_entry_id TEXT REFERENCES mcp_catalog(id);
-- ALTER nao aceita FK composta; os gatilhos mantem a credencial dentro da mesma empresa.
CREATE TRIGGER connections_credential_tenant_insert BEFORE INSERT ON connections
WHEN NEW.credential_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM credentials WHERE id = NEW.credential_ref AND tenant_id = NEW.tenant_id)
BEGIN SELECT RAISE(ABORT, 'credencial de outra empresa'); END;
CREATE TRIGGER connections_credential_tenant_update BEFORE UPDATE OF credential_ref, tenant_id ON connections
WHEN NEW.credential_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM credentials WHERE id = NEW.credential_ref AND tenant_id = NEW.tenant_id)
BEGIN SELECT RAISE(ABORT, 'credencial de outra empresa'); END;
