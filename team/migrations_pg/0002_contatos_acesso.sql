-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0002_contatos_acesso.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0002: contatos (identidade separada de enderecos) e concessoes de acesso default-deny.
CREATE TABLE contacts (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE TABLE contact_addresses (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  contact_id TEXT,
  address_key TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, address_key),
  FOREIGN KEY (tenant_id, contact_id) REFERENCES contacts(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE TABLE contact_links (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  contact_id TEXT,
  department_id TEXT,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, contact_id, department_id),
  FOREIGN KEY (tenant_id, contact_id) REFERENCES contacts(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE,
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE TABLE access_grants (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX access_grants_by_principal ON access_grants (tenant_id, principal_id);
