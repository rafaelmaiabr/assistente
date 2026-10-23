-- 0020: lacunas de capacidade (pedidos que a equipe nao sabe atender) e a decisao do operador.
CREATE TABLE capability_gaps (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  memory_id TEXT,
  state TEXT NOT NULL,
  topic TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE INDEX capability_gaps_by_topic ON capability_gaps (tenant_id, state, topic);
