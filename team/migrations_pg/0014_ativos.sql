-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0014_ativos.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0014: ativos (dominios/hosts) que a empresa autoriza para diagnostico de rede.
CREATE TABLE monitored_assets (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  hostname TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, hostname)
);
