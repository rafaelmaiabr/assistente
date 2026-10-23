-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0017_entrada_emails.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0017: entrada de emails como demandas: cursor por caixa/pasta (UIDVALIDITY + UID) e registro de cada mensagem
-- ingerida ou ignorada, para deduplicar, encadear respostas e rastrear ate a mensagem fonte.
CREATE TABLE mailbox_cursors (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  department_id TEXT NOT NULL,
  folder TEXT NOT NULL,
  status TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, connection_id, folder),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connections(tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id)
);
CREATE TABLE email_messages (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  folder TEXT NOT NULL,
  uidvalidity BIGINT NOT NULL,
  uid BIGINT NOT NULL,
  message_id TEXT,
  case_id TEXT,
  classification TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, connection_id, folder, uidvalidity, uid),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connections(tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE INDEX email_messages_by_message_id ON email_messages (tenant_id, connection_id, message_id);
CREATE INDEX email_messages_by_case ON email_messages (tenant_id, case_id);
