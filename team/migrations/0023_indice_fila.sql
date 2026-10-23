-- 0023: a fila conta as execucoes em andamento por empresa e por tipo (cotas e justica); o indice parcial mantem isso barato.
CREATE INDEX jobs_running ON jobs (tenant_id, kind) WHERE state = 'em_execucao';
