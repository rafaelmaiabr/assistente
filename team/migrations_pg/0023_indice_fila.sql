-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0023_indice_fila.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0023: a fila conta as execucoes em andamento por empresa e por tipo (cotas e justica); o indice parcial mantem isso barato.
CREATE INDEX jobs_running ON jobs (tenant_id, kind) WHERE state = 'em_execucao';
