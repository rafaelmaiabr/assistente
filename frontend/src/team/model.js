// Pure helpers for the team panel: forms <-> API payloads, validation and messages. No I/O.

export const lines = text => String(text ?? '').split('\n').map(line => line.trim()).filter(Boolean);
export const joinLines = list => (list || []).join('\n');
export const commaList = text => String(text ?? '').split(',').map(item => item.trim()).filter(Boolean);

export const blankAgent = departmentId => ({
  department_id: departmentId || '', name: '', goals: '', tone: '', initiative: '', scope: '', out_of_scope: '',
  routing_policy: 'encaminhar', model_policy_ref: 'local-primeiro', voice_profile_ref: '', handoff_target_ids: [],
  capabilities: '', status: 'ativo',
});

export function agentDraft(agent) {
  return {
    department_id: agent.department_id, name: agent.name, goals: joinLines(agent.goals), tone: agent.personality.tone,
    initiative: agent.personality.initiative, scope: agent.scope, out_of_scope: agent.out_of_scope,
    routing_policy: agent.routing_policy || 'encaminhar', model_policy_ref: agent.model_policy_ref,
    voice_profile_ref: agent.voice_profile_ref || '', handoff_target_ids: [...(agent.handoff_target_ids || [])],
    capabilities: (agent.capabilities || []).join(', '), status: agent.status,
  };
}

export function agentPayload(draft) {
  return {
    department_id: draft.department_id, name: draft.name.trim(), goals: lines(draft.goals),
    personality: {tone: draft.tone.trim(), initiative: draft.initiative.trim()}, scope: draft.scope.trim(),
    out_of_scope: draft.out_of_scope.trim(), routing_policy: draft.routing_policy,
    model_policy_ref: draft.model_policy_ref, voice_profile_ref: draft.voice_profile_ref || null,
    handoff_target_ids: draft.handoff_target_ids, capabilities: commaList(draft.capabilities),
  };
}

const LABELS = {name: 'Nome', tone: 'Tom', initiative: 'Iniciativa', scope: 'Escopo', out_of_scope: 'Fora do escopo',
  department_id: 'Departamento', model_policy_ref: 'Política de modelo'};

export function validateAgent(draft) {
  const problems = [];
  for (const key of ['name', 'tone', 'initiative', 'scope', 'out_of_scope', 'department_id', 'model_policy_ref']) {
    if (!String(draft[key] ?? '').trim()) problems.push(`${LABELS[key]} é obrigatório.`);
  }
  if (draft.name.length > 120) problems.push('Nome muito longo (até 120 caracteres).');
  return problems;
}

export function changedFields(original, draft) {
  const before = agentDraft(original);
  return Object.keys(before).filter(key => JSON.stringify(before[key]) !== JSON.stringify(draft[key]));
}

export function describeError(error) {
  if (!error) return '';
  if (error.code === 'persistence_disabled') return 'Ative o atendimento persistente para usar a equipe.';
  if (error.code === 'conflict') return 'Os dados mudaram em outro lugar. Recarregue e refaça a alteração.';
  if (error.status === 404) return 'Não encontrado. A empresa ou o item pode não existir mais.';
  if (error.details?.length) return `${error.message} ${error.details.slice(0, 3).join('; ')}`;
  return error.message || 'Não foi possível concluir a operação.';
}

export function permissionLines(review) {
  const tools = review.permissions.map(p => `${p.tool} — ${p.operations.join(', ')}`);
  return tools.length ? tools : ['Nenhuma ferramenta externa: somente instruções de trabalho.'];
}

export function operationsLabel(operations) {
  const names = {read: 'leitura', write: 'escrita', send: 'envio', spend: 'pagamento'};
  return (operations || []).map(o => names[o] || o).join(', ');
}

export function credentialLabel(connection) {
  const c = connection.credential;
  if (!c) return 'Sem credencial';
  return c.state === 'ativa' ? `Credencial ativa (${c.store === 'cofre-local' ? 'cofre do Windows' : 'chave da voz'})` : 'Credencial revogada';
}

/** Never keep or echo a secret: callers pass the field value to the API and clear it right after. */
export const secretIsAcceptable = value => typeof value === 'string' && value.length >= 1 && value.length <= 4096
  && value === value.trim() && !/[\r\n]/.test(value);
