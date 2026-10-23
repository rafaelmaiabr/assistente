// Pure rules for a conversation attached to a company, a case and an agent. No I/O, no timers, no DOM.
//
// The server decides the binding when a turn starts and tags every event of that turn with its number and a compact
// binding. This model only decides which events still belong to the conversation on screen, so a late audio from another
// case or an event that arrives after its turn ended is dropped instead of being shown or played.

export const TURN_EVENTS = new Set(['status', 'message', 'audio', 'tool', 'warning', 'error']);
export const TERMINAL_PHASES = new Set(['concluida', 'cancelada', 'falha']);

export const initialConversation = () => ({
  binding: null,          // last binding announced by the server (null = the plain local assistant)
  selection: null,        // what the person asked to attach to: {company_id, case_id}; survives reconnection
  turn: 0,                // highest turn number seen
  running: false,         // a turn is generating
  turnBinding: null,      // binding captured when the running turn started; used to label that turn
  cancelledTurn: 0,       // events of a cancelled turn that still arrive are dropped
  sentAt: null, latencyMs: null, latencies: [],
});

const caseOf = binding => binding?.case?.id ?? binding?.case_id ?? null;
const companyOf = binding => binding?.company?.id ?? binding?.company_id ?? null;

/** True when an event's compact binding is for another company or case than the one on screen. */
export function foreignBinding(state, event) {
  if (!event.binding) return false;
  return caseOf(event.binding) !== caseOf(state.binding) || companyOf(event.binding) !== companyOf(state.binding);
}

/** The person pressed send: the turn is captured with the binding on screen at this instant. */
export function startTurn(state, now) {
  return {...state, running: true, turnBinding: state.binding, sentAt: now, latencyMs: null};
}

export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b), mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : Math.round((sorted[mid - 1] + sorted[mid]) / 2);
}

/**
 * Apply one server event. Returns {state, accept, effects}. `accept` says whether the UI may show or play it; `effects`
 * are things the UI must do (never turning the microphone on: that is only ever a person's click).
 */
export function applyEvent(state, event, now = 0) {
  if (event.type === 'binding') {
    return {state: {...state, binding: event.binding.mode === 'local' ? null : event.binding}, accept: true, effects: []};
  }
  if (event.type === 'cancelled') {
    return {state: {...state, running: false, cancelledTurn: state.turn}, accept: true, effects: []};
  }
  if (!TURN_EVENTS.has(event.type) || !Number.isInteger(event.turn)) return {state, accept: true, effects: []};  // legacy events
  if (event.turn < state.turn || event.turn <= state.cancelledTurn) return {state, accept: false, effects: []};  // late: its turn is over
  let next = state;
  if (event.turn > state.turn) next = {...state, turn: event.turn, running: true, turnBinding: state.binding};
  if ((event.type === 'message' || event.type === 'audio') && foreignBinding(next, event)) {
    return {state: next, accept: false, effects: []};  // an answer or a voice from another case or company
  }
  const effects = [];
  if (event.type === 'message' && event.role === 'assistant' && next.sentAt != null && next.latencyMs == null) {
    const latencyMs = Math.max(0, now - next.sentAt);
    next = {...next, latencyMs, latencies: [...next.latencies, latencyMs].slice(-20)};
  }
  if (event.type === 'audio') effects.push('play');
  if (event.type === 'status' && event.state === 'ready') next = {...next, running: false};
  if (event.type === 'error' && event.fatal) next = {...next, running: false};
  return {state: next, accept: true, effects};
}

/** After a reconnection the server forgets the attachment: keep what the person chose and ask to attach again. */
export function reconnected(state) {
  return {...initialConversation(), selection: state.selection, latencies: state.latencies};
}
export const reattachMessage = state => state.selection ? {type: 'attach', ...state.selection} : null;

export function select(state, selection) {
  return {...state, selection: selection ? {company_id: selection.company_id, case_id: selection.case_id} : null};
}

/** Which of the four different controls can be used right now. They never stand in for each other. */
export function controls(state, view) {
  const bound = state.binding?.case;
  return {
    stopSpeech: Boolean(view.speaking),                                      // stops playback only
    interrupt: Boolean(state.running && !view.fatal),                        // cancels the generation in progress
    pauseCase: Boolean(bound && !state.binding.paused && !TERMINAL_PHASES.has(bound.phase)),  // changes the case, not the call
    microphone: !view.fatal && !['offline', 'connecting'].includes(view.connection),  // capture on/off, by hand only
  };
}

export function bindingLabel(binding) {
  if (!binding || binding.mode === 'local') return 'Assistente local';
  const agent = binding.agent?.name || 'sem responsável';
  return `${binding.company?.name} · ${binding.case?.label} · ${agent}`;
}

/** What the desktop avatar should show. The conversation state wins; "waiting for a person" shows when nothing else is happening. */
export function avatarStatus({connection, micEnabled, speaking, level, binding}) {
  let state = connection;
  if (connection === 'ready') state = speaking ? 'speaking' : micEnabled ? 'listening' : 'ready';
  return {state, micEnabled: Boolean(micEnabled), level: level || 0, waitingHuman: Boolean(binding?.waiting_human),
    caseRef: binding?.case ? {company_id: binding.company.id, case_id: binding.case.id} : null,
    caseLabel: binding?.case ? binding.case.label : null, agentName: binding?.agent?.name || null};
}

/** A deep link to the case's screen, validated: ids must have the shape the backend generates. */
const ID = /^[a-z]{3}_[0-9a-f]{32}$/;
export const isCaseRef = ref => Boolean(ref && ID.test(ref.company_id) && ID.test(ref.case_id));
