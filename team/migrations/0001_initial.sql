-- 0001: contratos T01 persistidos. Isolamento por empresa garantido por FKs compostas (tenant_id, id).

CREATE TABLE tenants (
  id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc))
);
CREATE TABLE departments (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE TABLE skill_definitions (
  id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  skill_key TEXT NOT NULL,
  skill_version TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (skill_key, skill_version)
);
CREATE TABLE connections (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id)
);
CREATE TABLE agent_definitions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  department_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id)
);
CREATE TABLE skill_grants (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  agent_id TEXT,
  skill_id TEXT,
  connection_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agent_definitions(tenant_id, id),
  FOREIGN KEY (skill_id) REFERENCES skill_definitions(id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connections(tenant_id, id)
);
CREATE TABLE cases (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  department_id TEXT,
  owner_agent_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id),
  FOREIGN KEY (tenant_id, owner_agent_id) REFERENCES agent_definitions(tenant_id, id)
);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  case_id TEXT,
  parent_task_id TEXT,
  assignee_agent_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id),
  FOREIGN KEY (tenant_id, parent_task_id) REFERENCES tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, assignee_agent_id) REFERENCES agent_definitions(tenant_id, id)
);
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  agent_id TEXT,
  department_id TEXT,
  case_id TEXT,
  task_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agent_definitions(tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks(tenant_id, id)
);
CREATE TABLE approval_requests (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  case_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE TABLE handoffs (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL,
  case_id TEXT,
  task_id TEXT,
  from_agent_id TEXT,
  to_agent_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, from_agent_id) REFERENCES agent_definitions(tenant_id, id),
  FOREIGN KEY (tenant_id, to_agent_id) REFERENCES agent_definitions(tenant_id, id)
);
CREATE TABLE evidences (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);

-- Comandos: a chave de idempotencia impede reprocessar o mesmo pedido.
CREATE TABLE commands (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  case_id TEXT,
  idempotency_key TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);

-- Eventos: fatos comprometidos. seq fornece ordem global; revision ordena por demanda.
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  case_id TEXT,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  occurred_at TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id)
);
CREATE UNIQUE INDEX events_case_revision ON events (tenant_id, case_id, revision) WHERE case_id IS NOT NULL;

-- Outbox transacional: gravada na mesma transacao do estado e do evento.
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  event_seq INTEGER NOT NULL REFERENCES events(seq),
  topic TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dispatched')),
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  dispatched_at TEXT
);
CREATE INDEX outbox_pending ON outbox (status, id);

CREATE INDEX cases_by_state ON cases (tenant_id, state);
CREATE INDEX tasks_by_case ON tasks (tenant_id, case_id);
