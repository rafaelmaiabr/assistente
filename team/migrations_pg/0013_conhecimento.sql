-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0013_conhecimento.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0013: base de conhecimento (documentos versionados), memorias confirmadas e indice de busca local (FTS5).
CREATE TABLE knowledge_documents (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  department_id TEXT,
  doc_key TEXT NOT NULL,
  status TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX knowledge_by_key ON knowledge_documents (tenant_id, doc_key, created_at);
CREATE TABLE memory_items (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  department_id TEXT,
  state TEXT NOT NULL,
  scope TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE TABLE knowledge_index (title TEXT NOT NULL, body TEXT NOT NULL, tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, tsv TSVECTOR NOT NULL);
CREATE INDEX knowledge_index_tsv ON knowledge_index USING GIN (tsv);
CREATE INDEX knowledge_index_doc ON knowledge_index (doc_id);
