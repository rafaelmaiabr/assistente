// Moving messages to Trash or Spam (#140). The server owns the proposals and the result; this mirrors them and builds the person's click.
// The click carries the hash of exactly what the card shows, so a changed proposal cannot be approved. The model cannot send these messages.
export const initialMoves = {proposals: [], status: {}};

const SUMMARY = {
  descartada: () => 'Cancelado. Nada foi movido.',
  concluida: event => `Movidas ${event.movidas} mensagem(ns) para ${event.destino}.`,
  parada: event => `Lote parado: ${event.erro || 'uma verificação falhou'} Movidas até aqui: ${event.movidas}.`,
};

export function applyMoveEvent(state, event) {
  if (event.type === 'move_proposals' && Array.isArray(event.proposals)) {
    return {...state, proposals: event.proposals};
  }
  if (event.type === 'move_proposal' && event.proposal && !state.proposals.some(item => item.id === event.proposal.id)) {
    return {...state, proposals: [...state.proposals, event.proposal]};
  }
  if (event.type === 'move_result' && typeof event.id === 'string') {
    if (event.estado === 'movendo') return {...state, status: {...state.status, [event.id]: 'Movendo, uma mensagem por vez…'}};
    const text = (SUMMARY[event.estado] || (() => event.erro || 'Nada foi movido.'))(event);
    return {proposals: state.proposals.filter(item => item.id !== event.id), status: {...state.status, [event.id]: text}};
  }
  return state;
}

export const isMoveEvent = event => ['move_proposal', 'move_proposals', 'move_result'].includes(event.type);

export function decisionMessage(proposal, decision) {
  if (!proposal || typeof proposal.id !== 'string' || !['approve', 'discard'].includes(decision)) return null;
  return decision === 'approve' ? {type: 'move_decision', id: proposal.id, decision, hash: proposal.hash} : {type: 'move_decision', id: proposal.id, decision};
}
