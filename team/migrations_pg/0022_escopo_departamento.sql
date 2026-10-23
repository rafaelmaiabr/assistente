-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0022_escopo_departamento.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0022: escopo opcional por departamento para quem entra com Google (NULL = a empresa inteira).
ALTER TABLE auth_memberships ADD COLUMN departments JSONB;
