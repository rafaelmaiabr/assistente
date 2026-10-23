-- 0011: aprovacoes: nao ha duas solicitacoes abertas para o mesmo payload exato na mesma demanda.
CREATE UNIQUE INDEX approvals_one_open_per_payload ON approval_requests
  (tenant_id, case_id, json_extract(doc, '$.payload_hash')) WHERE state IN ('pendente', 'aprovada');
