-- 0006: politicas de roteamento de modelos por empresa (provedor externo desligado por padrao).
CREATE TABLE model_policies (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
