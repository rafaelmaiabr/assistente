-- 0019: aplicativos OAuth registrados pelo operador (segredo no cofre) e concessoes por conexao (so metadados).
CREATE TABLE oauth_apps (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  status TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, provider)
);
CREATE TABLE oauth_grants (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  state TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, connection_id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connections(tenant_id, id),
  FOREIGN KEY (tenant_id, app_id) REFERENCES oauth_apps(tenant_id, id)
);
