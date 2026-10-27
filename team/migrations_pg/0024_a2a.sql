-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0024_a2a.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0024: tarefas entregues a agentes externos (A2A). A demanda continua com o seu dono; este registro so acompanha a tarefa remota.
-- Uma mensagem (message_id) vira no maximo uma tarefa e uma tarefa remota pertence a uma unica conexao: repetir nao duplica.
CREATE TABLE a2a_tasks (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id) DEFERRABLE INITIALLY IMMEDIATE,
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  state TEXT NOT NULL,
  message_id TEXT NOT NULL,
  remote_task_id TEXT,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, message_id),
  FOREIGN KEY (tenant_id, case_id) REFERENCES cases(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE,
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connections(tenant_id, id) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE UNIQUE INDEX a2a_tasks_by_remote ON a2a_tasks (tenant_id, connection_id, remote_task_id) WHERE remote_task_id IS NOT NULL;
CREATE INDEX a2a_tasks_by_case ON a2a_tasks (tenant_id, case_id, state);
