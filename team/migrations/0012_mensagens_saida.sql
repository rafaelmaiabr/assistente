-- 0012: mensagens de saida (rascunho separado do envio; a tabela serve de outbox).
CREATE TABLE outbound_messages (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  account_id TEXT,
  owner_agent_id TEXT,
  approval_id TEXT,
  state TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  provider_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, dedup_key),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id),
  FOREIGN KEY (tenant_id, account_id) REFERENCES connections(tenant_id, id),
  FOREIGN KEY (tenant_id, owner_agent_id) REFERENCES agent_definitions(tenant_id, id),
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approval_requests(tenant_id, id)
);
-- Serializacao por demanda: no maximo um envio em andamento por vez.
CREATE UNIQUE INDEX outbound_one_sending_per_case ON outbound_messages (tenant_id, case_id) WHERE state = 'enviando';
-- Um identificador do provedor nao pode estar em duas mensagens da mesma empresa.
CREATE UNIQUE INDEX outbound_provider_id ON outbound_messages (tenant_id, provider_id) WHERE provider_id IS NOT NULL;
CREATE INDEX outbound_by_case ON outbound_messages (tenant_id, case_id, created_at);
