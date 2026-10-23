-- 0009: encaminhamentos entre agentes: retorno, idempotencia e uma transferencia pendente por demanda.
ALTER TABLE handoffs ADD COLUMN return_to TEXT REFERENCES agent_definitions(id);
CREATE UNIQUE INDEX handoffs_idempotency ON handoffs (tenant_id, json_extract(doc, '$.idempotency_key'));
-- Enquanto uma transferencia aguarda aceite, nao pode haver outra proposta para a mesma demanda.
CREATE UNIQUE INDEX handoffs_one_pending_transfer ON handoffs (tenant_id, case_id)
  WHERE state = 'proposto' AND json_extract(doc, '$.handoff_kind') = 'transferencia';
