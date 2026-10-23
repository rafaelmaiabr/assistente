// Pure helpers for the cases center: event cursor handling, labels and approval summaries. No I/O.

export const PHASE_LABEL = {
  andamento: 'Em andamento', aguardando_cliente: 'Aguardando o cliente', aguardando_humano: 'Aguardando uma pessoa',
  falha: 'Falhou', concluida: 'Concluída', cancelada: 'Cancelada',
};
export const PHASE_CLASS = {andamento: 'ok', aguardando_cliente: 'wait', aguardando_humano: 'human', falha: 'off', concluida: 'done', cancelada: 'off'};

/** Merge events from the server into what we know: dedupe by id, order by seq, never move the cursor backwards. */
export function mergeEvents(known, incoming, cursor = 0) {
  const byId = new Map(known.map(e => [e.id, e]));
  for (const event of incoming) if (!byId.has(event.id)) byId.set(event.id, event);
  const events = [...byId.values()].sort((a, b) => a.seq - b.seq);
  return {events, cursor: events.reduce((max, e) => Math.max(max, e.seq), cursor)};
}

/** True when a batch of events means the list or the open case must be re-read from the server. */
export const needsRefresh = events => events.length > 0;
export const touchesCase = (events, caseId) => events.some(e => e.case_id === caseId);

/** Poll delay: steady while healthy, exponential (capped) while the connection is failing. */
export const nextDelay = (failures, base = 3000, cap = 30000) => failures <= 0 ? base : Math.min(cap, base * 2 ** failures);

export function formatMoney(cents, currency) {
  if (cents == null) return '';
  const value = (cents / 100).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const symbol = {brl: 'R$', usd: 'US$', eur: '€'}[String(currency || '').toLowerCase()];
  return symbol ? `${symbol} ${value}` : `${value} ${String(currency || '').toUpperCase()}`.trim();
}

const ACTIONS = {enviar_mensagem: 'Enviar mensagem ao cliente', enviar_email: 'Enviar email', renovar_dominio: 'Renovar domínio',
  emitir_cobranca: 'Emitir cobrança', pagamento: 'Efetuar pagamento'};
export const actionLabel = action => ACTIONS[action] || (action?.startsWith('ferramenta:') ? `Usar ferramenta ${action.split(':').pop()}` : action);

export function approvalLines(approval) {
  const lines = [['Ação', actionLabel(approval.action)]];
  const to = approval.recipient?.to || [];
  if (to.length) lines.push(['Destinatário(s)', to.join(', ')]);
  if (approval.amount_cents != null) lines.push(['Valor', formatMoney(approval.amount_cents, approval.currency)]);
  if (approval.resource) lines.push(['Recurso', approval.resource]);
  for (const [key, value] of Object.entries(approval.params || {})) lines.push([key, typeof value === 'string' ? value : JSON.stringify(value)]);
  lines.push(['Válida até', approval.expires_at]);
  return lines;
}
export const shortHash = hash => `${hash.slice(0, 8)}…${hash.slice(-6)}`;
export const canDecide = approval => approval.state === 'pendente';
export const canRevoke = approval => approval.state === 'pendente' || approval.state === 'aprovada';

const EVENTS = {
  'case.opened': 'Demanda recebida', 'case.state-changed': 'Estado alterado', 'case.owner-assigned': 'Responsável definido',
  'case.transferred': 'Responsabilidade transferida', 'case.reopened': 'Demanda reaberta', 'case.subtask-returned': 'Subtarefa devolvida',
  'case.operator-took-over': 'O operador assumiu', 'case.returned-to-team': 'Devolvida à equipe', 'handoff.proposed': 'Encaminhamento proposto',
  'handoff.accepted': 'Encaminhamento aceito', 'handoff.declined': 'Encaminhamento recusado', 'handoff.expired': 'Encaminhamento expirado',
  'handoff.cancelled': 'Encaminhamento cancelado', 'handoff.failed': 'Subtarefa falhou', 'approval.requested': 'Aprovação solicitada',
  'approval.approved': 'Aprovação concedida', 'approval.denied': 'Aprovação negada', 'approval.revoked': 'Aprovação revogada',
  'approval.expired': 'Aprovação expirada',
};
export const describeEvent = type => EVENTS[type] || type;

export const STATE_LABEL = {recebida: 'Recebida', triagem: 'Triagem', em_execucao: 'Em execução', aguardando_cliente: 'Aguardando cliente',
  aguardando_aprovacao: 'Aguardando aprovação', aguardando_subtarefa: 'Aguardando subtarefa', pausada: 'Pausada', resolvida: 'Resolvida',
  falhou: 'Falhou', cancelada: 'Cancelada'};
