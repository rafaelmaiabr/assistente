// Mailbox rules (#139). The server owns them: this only mirrors what the server says and builds the person's own messages.
// The model cannot produce any of these messages; a rule it proposes appears as a card that only the person's choice resolves.
export const initialRules = {rules: [], pending: [], message: ''};

export const ACTIONS = [
  {id: 'prioridade', label: 'Tratar como prioridade'},
  {id: 'preservar', label: 'Preservar (conferir você)'},
  {id: 'mover_lixeira', label: 'Mover para a Lixeira (ainda inativa)'},
];
const DECIDED = {
  criada: 'Regra criada. Ela vale a partir de agora e nas próximas conversas.',
  recusada: 'Proposta recusada. Nada foi salvo.',
  uma_vez: 'Regra aplicada só desta vez. Nada foi salvo.',
};
export const STATUS_LABEL = {ativa: 'Ativa', desativada: 'Desativada', substituida: 'Substituída'};

export function applyRulesEvent(state, event) {
  if (event.type === 'rules') {
    return {
      rules: Array.isArray(event.rules) ? event.rules : state.rules,
      pending: Array.isArray(event.pending) ? event.pending : state.pending,
      message: event.ok === false ? String(event.erro || 'Não foi possível concluir.') : DECIDED[event.decidida] || '',
    };
  }
  if (event.type === 'rule_proposal' && event.proposal && !state.pending.some(item => item.id === event.proposal.id)) {
    return {...state, pending: [...state.pending, event.proposal], message: ''};
  }
  return state;
}

export const isRulesEvent = event => event.type === 'rules' || event.type === 'rule_proposal';

export const decisionMessage = (id, decision) =>
  id && ['criar', 'uma_vez', 'recusar'].includes(decision) ? {type: 'rule_decision', id, decision} : null;
export const toggleMessage = (id, active) => id && typeof active === 'boolean' ? {type: 'rule_toggle', id, active} : null;

// The exceptions are typed as "a; b"; the server checks every one of them again.
export function editMessage(id, {valor, excecoes, acao}) {
  if (!id) return null;
  const list = String(excecoes || '').split(';').map(item => item.trim()).filter(Boolean);
  const message = {type: 'rule_edit', id, excecoes: list};
  if (typeof valor === 'string' && valor.trim()) message.valor = valor.trim();
  if (ACTIONS.some(item => item.id === acao)) message.acao = acao;
  return message;
}
