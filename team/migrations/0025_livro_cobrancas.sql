-- 0025: livro de cobrancas do ERP (#110). Estado reconciliado de cada cobranca, eventos ja aplicados (idempotencia) e pedidos de emissao
-- cujo resultado pode ser desconhecido. A unicidade (empresa, tipo, fornecedor, chave) impede aplicar o mesmo evento ou a mesma chave duas vezes.
CREATE TABLE erp_ledger_entries (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  namespace TEXT NOT NULL CHECK (namespace IN ('charge', 'event', 'emission')),
  provider TEXT NOT NULL,
  entry_key TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, namespace, provider, entry_key)
);
