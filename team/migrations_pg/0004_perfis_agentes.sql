-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0004_perfis_agentes.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0004: versoes imutaveis de perfis de agentes; execucoes so iniciam em versao existente e ativa.
CREATE TABLE agent_profile_versions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  revision BIGINT NOT NULL CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_version BIGINT NOT NULL CHECK (agent_version >= 1),
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  doc JSONB NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, agent_id, agent_version),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agent_definitions(tenant_id, id)
);
ALTER TABLE agent_runs ADD COLUMN agent_version BIGINT;
CREATE FUNCTION agent_profile_versions_frozen_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.agent_id IS DISTINCT FROM OLD.agent_id OR NEW.agent_version IS DISTINCT FROM OLD.agent_version
  OR NEW.content_hash IS DISTINCT FROM OLD.content_hash OR (NEW.doc::jsonb -> 'definition') IS DISTINCT FROM (OLD.doc::jsonb -> 'definition') THEN RAISE EXCEPTION 'versao de perfil e imutavel' USING ERRCODE = '23000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER agent_profile_versions_frozen BEFORE UPDATE ON agent_profile_versions FOR EACH ROW EXECUTE FUNCTION agent_profile_versions_frozen_fn();
CREATE FUNCTION agent_profile_versions_no_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'versao de perfil nao pode ser removida' USING ERRCODE = '23000'; RETURN OLD; END $$;
CREATE TRIGGER agent_profile_versions_no_delete BEFORE DELETE ON agent_profile_versions FOR EACH ROW EXECUTE FUNCTION agent_profile_versions_no_delete_fn();
CREATE FUNCTION agent_runs_version_active_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NOT EXISTS (SELECT 1 FROM agent_profile_versions v WHERE v.tenant_id = NEW.tenant_id
                 AND v.agent_id = NEW.agent_id AND v.agent_version = NEW.agent_version AND v.status = 'ativo') THEN RAISE EXCEPTION 'versao do agente inexistente ou desativada' USING ERRCODE = '23000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER agent_runs_version_active BEFORE INSERT ON agent_runs FOR EACH ROW EXECUTE FUNCTION agent_runs_version_active_fn();
