// The person's memory (#137). The server owns it: this only mirrors what the server says and builds the person's own messages.
// The model cannot produce any of these messages; a proposal it makes appears as a card that only a click decides.
export const MAX_ENTRY = 200;
export const initialMemory = {entries: [], pending: [], used: 0, limit: 1400, message: ''};

const DECIDED = {aprovada: 'Anotação aprovada. Ela passa a valer na próxima conversa.', descartada: 'Proposta descartada.'};

export function applyMemoryEvent(state, event) {
  if (event.type === 'memory') {
    return {
      entries: Array.isArray(event.entries) ? event.entries : state.entries,
      pending: Array.isArray(event.pending) ? event.pending : state.pending,
      used: Number.isInteger(event.used) ? event.used : state.used,
      limit: Number.isInteger(event.limit) ? event.limit : state.limit,
      message: event.ok === false ? String(event.erro || 'Não foi possível guardar.') : DECIDED[event.decidida] || '',
    };
  }
  if (event.type === 'memory_proposal' && event.proposal && !state.pending.some(item => item.id === event.proposal.id)) {
    return {...state, pending: [...state.pending, event.proposal], message: ''};
  }
  return state;
}

export const isMemoryEvent = event => event.type === 'memory' || event.type === 'memory_proposal';

export function validText(text) {
  const clean = String(text || '').trim();
  return clean.length > 0 && clean.length <= MAX_ENTRY;
}

export const addMessage = text => validText(text) ? {type: 'memory_add', text: text.trim()} : null;
export const editMessage = (id, text) => id && validText(text) ? {type: 'memory_edit', id, text: text.trim()} : null;
export const deleteMessage = id => id ? {type: 'memory_delete', id} : null;
export const decisionMessage = (id, decision) => id && (decision === 'approve' || decision === 'discard') ? {type: 'memory_decision', id, decision} : null;

// What a proposal asks for, in words the person reads before clicking.
export function describeProposal(proposal) {
  if (proposal.action === 'remover') return `Esquecer: “${proposal.current ?? ''}”`;
  if (proposal.action === 'ajustar') return `Trocar “${proposal.current ?? ''}” por “${proposal.text}”`;
  return `Lembrar: “${proposal.text}”`;
}
