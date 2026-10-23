-- 0004: versoes imutaveis de perfis de agentes; execucoes so iniciam em versao existente e ativa.
CREATE TABLE agent_profile_versions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_version INTEGER NOT NULL CHECK (agent_version >= 1),
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  doc TEXT NOT NULL CHECK (json_valid(doc)),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, agent_id, agent_version),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agent_definitions(tenant_id, id)
);
ALTER TABLE agent_runs ADD COLUMN agent_version INTEGER;
-- O conteudo de uma versao nunca muda; apenas o status (desativar) pode ser atualizado.
CREATE TRIGGER agent_profile_versions_frozen BEFORE UPDATE ON agent_profile_versions
WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.agent_version IS NOT OLD.agent_version
  OR NEW.content_hash IS NOT OLD.content_hash OR json_extract(NEW.doc, '$.definition') IS NOT json_extract(OLD.doc, '$.definition')
BEGIN SELECT RAISE(ABORT, 'versao de perfil e imutavel'); END;
CREATE TRIGGER agent_profile_versions_no_delete BEFORE DELETE ON agent_profile_versions
BEGIN SELECT RAISE(ABORT, 'versao de perfil nao pode ser removida'); END;
CREATE TRIGGER agent_runs_version_active BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_profile_versions v WHERE v.tenant_id = NEW.tenant_id
                 AND v.agent_id = NEW.agent_id AND v.agent_version = NEW.agent_version AND v.status = 'ativo')
BEGIN SELECT RAISE(ABORT, 'versao do agente inexistente ou desativada'); END;
