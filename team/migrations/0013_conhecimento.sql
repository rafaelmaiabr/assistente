-- 0013: base de conhecimento (documentos versionados), memorias confirmadas e indice de busca local (FTS5).
CREATE TABLE knowledge_documents (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  department_id TEXT,
  doc_key TEXT NOT NULL,
  status TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id)
);
CREATE INDEX knowledge_by_key ON knowledge_documents (tenant_id, doc_key, created_at);
CREATE TABLE memory_items (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  department_id TEXT,
  state TEXT NOT NULL,
  scope TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id)
);
-- Somente documentos publicados e vigentes ficam no indice; revogar, expirar ou excluir o remove na hora.
CREATE VIRTUAL TABLE knowledge_index USING fts5(
  title, body, tenant_id UNINDEXED, doc_id UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
