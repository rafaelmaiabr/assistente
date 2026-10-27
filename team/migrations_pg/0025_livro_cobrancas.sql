-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0025_livro_cobrancas.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0025: livro de cobrancas do ERP (#110). Estado reconciliado de cada cobranca, eventos ja aplicados (idempotencia) e pedidos de emissao
-- cujo resultado pode ser desconhecido. A unicidade (empresa, tipo, fornecedor, chave) impede aplicar o mesmo evento ou a mesma chave duas vezes.
CREATE TABLE erp_ledger_entries (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  namespace TEXT NOT NULL CHECK (namespace IN ('charge', 'event', 'emission')),
  provider TEXT NOT NULL,
  entry_key TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, namespace, provider, entry_key)
);
