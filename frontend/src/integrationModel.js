// Pure rules for connecting a system from the conversation. The cards and their buttons are backend decisions; this only keeps what
// the server sent, builds the messages a click produces, and words what is on screen. No I/O, no DOM, and nothing here stores a secret:
// the form values live in the form component and are cleared after every send.
import {formPayload} from './team/connectorsModel.js';

export const initialIntegrations = () => ({cards: [], forms: {}, status: {}});
const MAX_CARDS = 5;
export const cardKey = card => card?.request_id || 'persistencia';

/** Events that are not for the conversation history: they only update the integration cards. */
export const isIntegrationEvent = event => typeof event?.type === 'string' && event.type.startsWith('integration_');

export function applyIntegrationEvent(state, event) {
  switch (event.type) {
    case 'integration_card': {
      if (!event.card || typeof event.card.state !== 'string') return state;
      const key = cardKey(event.card);
      const others = state.cards.filter(card => cardKey(card) !== key);
      const kept = [...others, event.card].slice(-MAX_CARDS);
      const alive = new Set(kept.map(cardKey));
      const forms = Object.fromEntries(Object.entries(state.forms).filter(([id]) => alive.has(id)));
      const status = Object.fromEntries(Object.entries(state.status).filter(([id]) => alive.has(id)));
      return {cards: kept, forms, status};
    }
    case 'integration_form':
      return event.form?.request_id ? {...state, forms: {...state.forms, [event.form.request_id]: event.form}, status: {...state.status, [event.form.request_id]: {}}} : state;
    case 'integration_busy':
      return {...state, status: {...state.status, [event.request_id]: {busy: true, text: event.text || 'Aguarde…'}}};
    case 'integration_oauth':
      return {...state, status: {...state.status, [event.request_id]: {busy: true, waiting: true, url: safeAuthUrl(event.url) ? event.url : '',
        text: 'Aguardando você concluir a autorização no navegador…'}}};
    case 'integration_result': {
      const status = {busy: false, ok: Boolean(event.ok), message: event.message || '', next: event.next || null, resume: Boolean(event.resume)};
      const forms = event.ok && !event.next ? Object.fromEntries(Object.entries(state.forms).filter(([id]) => id !== event.request_id)) : state.forms;
      return {...state, forms, status: {...state.status, [event.request_id]: status}};
    }
    case 'integration_error':
      return {...state, status: {...state.status, [event.request_id || 'geral']: {busy: false, ok: false, message: event.message || 'Não foi possível concluir.'}}};
    default:
      return state;
  }
}

/** The address of a browser sign-in page: only https (or a loopback address for tests), never credentials in the URL. */
export function safeAuthUrl(url) {
  try {
    const parsed = new URL(url);
    const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname);
    return (parsed.protocol === 'https:' || (parsed.protocol === 'http:' && loopback)) && !parsed.username && !parsed.password && url.length <= 2000;
  } catch {
    return false;
  }
}

/** What each company row offers. "Existing" needs the person to confirm; a broken one is reconnected; a new one is connected. */
export function rowActions(row) {
  if (row.state === 'existente') return [{id: 'use', label: 'Usar esta conexão'}, {id: 'reconfigure', label: 'Reconfigurar'}];
  if (row.state === 'reconectar') return [{id: 'reconnect', label: 'Reconectar'}];
  return [{id: 'connect', label: 'Conectar'}];
}

export function rowText(row) {
  if (row.state === 'existente') return `Já conectado: ${row.connection.label}.`;
  if (row.state === 'reconectar') return `A conexão “${row.connection.label}” ${row.reason || 'precisa ser refeita'}.`;
  return 'Ainda não conectado.';
}

/** The sentence of a card that has no buttons to press, or '' when it has rows. */
export function cardNotice(card) {
  if (card.state === 'indisponivel') {
    const available = (card.available || []).join(', ');
    return `Não há um conector instalado para “${card.wanted || 'esse sistema'}”.${available ? ` Disponíveis agora: ${available}.` : ''}`;
  }
  if (card.state === 'escolher') return 'Qual destes você quer conectar?';
  if (card.state === 'sem_empresa') return 'Crie uma empresa na seção Equipe antes de conectar um sistema.';
  if (card.state === 'persistencia_desligada') return 'Ative o atendimento persistente na seção Equipe para conectar sistemas.';
  return '';
}

// -- the messages a click produces (the model cannot send these) -------------------------------------------------------------------------
export const openMessage = (card, row) => ({type: 'integration_open', request_id: card.request_id, company_id: row.company_id});
export const useMessage = (card, row) => ({type: 'integration_use', request_id: card.request_id, company_id: row.company_id});
export const pickMessage = (card, provider) => ({type: 'integration_pick', request_id: card.request_id, provider});
export const oauthMessage = form => ({type: 'integration_oauth', request_id: form.request_id, company_id: form.company_id});
export const cancelMessage = requestId => ({type: 'integration_cancel', request_id: requestId});
export const submitMessage = (form, label, values) => ({type: 'integration_submit', request_id: form.request_id, company_id: form.company_id,
  ...(form.editing ? {} : {label: (label || form.label).trim()}), values: formPayload(form.fields, values)});

/** The text sent for the person once a connection works, so the assistant resumes what was asked before. */
export const resumeText = label => `Conclui a conexão com ${label}. Pode continuar com o que eu pedi.`;
export const shouldResume = status => Boolean(status?.ok && status.resume && !status.next);
