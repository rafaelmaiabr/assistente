-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/0011_aprovacoes.sql. Nao edite: altere a migracao SQLite e gere de novo.
-- 0011: aprovacoes: nao ha duas solicitacoes abertas para o mesmo payload exato na mesma demanda.
CREATE UNIQUE INDEX approvals_one_open_per_payload ON approval_requests
  (tenant_id, case_id, (doc->>'payload_hash')) WHERE state IN ('pendente', 'aprovada');
