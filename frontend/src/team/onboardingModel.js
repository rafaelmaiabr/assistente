// Pure rules for the guided setup and the installation check. No I/O.

/** The preview is only good for the template and name it was made for; changing either invalidates it. */
export const previewMatches = (plan, templateId, companyName) =>
  Boolean(plan && plan.template.id === templateId && plan.company.name === companyName.trim());

export const canPreview = (templateId, companyName) => Boolean(templateId && companyName.trim());
export const canCreate = (plan, templateId, companyName, busy) => !busy && previewMatches(plan, templateId, companyName);

/** One short sentence per kind of thing the preview says will be created, in the order a person reads them. */
export function planSummary(plan) {
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  return [
    `Empresa: ${plan.company.name}`,
    count(plan.departments.length, 'departamento', 'departamentos'),
    count(plan.agents.length, 'agente', 'agentes'),
    count(plan.capabilities.length, 'capacidade de triagem', 'capacidades de triagem'),
    count(plan.handoffs.length, 'permissão de encaminhamento', 'permissões de encaminhamento'),
    count(plan.case_access.length, 'agente com acesso às demandas', 'agentes com acesso às demandas'),
  ];
}

export const STATE_CLASS = {ok: 'ok', aviso: 'wait', erro: 'off'};
export const STATE_LABEL = {ok: 'Tudo certo', aviso: 'Atenção', erro: 'Corrigir'};

/** Errors first, then warnings, then what is fine; the list order inside each group is kept. */
export function sortedChecks(report) {
  const order = {erro: 0, aviso: 1, ok: 2};
  return [...report.itens].sort((a, b) => order[a.estado] - order[b.estado]);
}

export const doctorHeadline = report => {
  const errors = report.itens.filter(i => i.estado === 'erro').length, warnings = report.itens.filter(i => i.estado === 'aviso').length;
  if (errors) return `${errors} problema(s) a corrigir${warnings ? ` e ${warnings} aviso(s)` : ''}.`;
  return warnings ? `Sem problemas; ${warnings} aviso(s) para ver.` : 'Tudo certo com a instalação.';
};
