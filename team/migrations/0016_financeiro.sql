-- 0016: registros financeiros importados explicitamente (dados do cliente, nunca inferidos).
CREATE TABLE financial_records (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  counterparty_key TEXT NOT NULL,
  document_number TEXT,
  batch_id TEXT NOT NULL,
  status TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE INDEX financial_by_party ON financial_records (tenant_id, counterparty_key, record_kind);
CREATE INDEX financial_by_document ON financial_records (tenant_id, document_number);
