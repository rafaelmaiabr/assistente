-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0006_politicas_modelo.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0006: politicas de roteamento de modelos por empresa (provedor externo desligado por padrao).
CREATE TABLE model_policies (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
