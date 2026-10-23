-- 0021: identidade para acesso remoto: usuarios (login Google), papel por empresa, sessoes e trilha de autenticacao.
CREATE TABLE auth_users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  google_sub TEXT UNIQUE,
  display_name TEXT,
  status TEXT NOT NULL CHECK (status IN ('ativo', 'inativo')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_login_at TEXT
);
CREATE TABLE auth_memberships (
  user_id TEXT NOT NULL REFERENCES auth_users(id),
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  role TEXT NOT NULL CHECK (role IN ('leitor', 'atendente', 'aprovador', 'admin')),
  status TEXT NOT NULL CHECK (status IN ('ativo', 'inativo')),
  granted_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, tenant_id)
);
CREATE INDEX auth_memberships_by_tenant ON auth_memberships (tenant_id, status);
-- Somente o hash do token fica guardado: o valor so existe no cookie do navegador.
CREATE TABLE auth_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES auth_users(id),
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  revoked_reason TEXT
);
CREATE INDEX auth_sessions_by_user ON auth_sessions (user_id, revoked_at);
CREATE TABLE auth_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  user_id TEXT,
  tenant_id TEXT,
  actor_id TEXT,
  detail TEXT
);
