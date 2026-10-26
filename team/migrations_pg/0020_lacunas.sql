-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0020_lacunas.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0020: lacunas de capacidade (pedidos que a equipe nao sabe atender) e a decisao do operador.
CREATE TABLE capability_gaps (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  memory_id TEXT,
  state TEXT NOT NULL,
  topic TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX capability_gaps_by_topic ON capability_gaps (tenant_id, state, topic);
