// Pure rules for the email skill on screen: the secure setup form, the draft cards and the skill chip. No I/O, no DOM.
//
// Two things are decided by the person and nowhere else: setting the account up (the password goes from the form to the
// server and never through the model) and approving a draft (the click sends the hash of what was on screen).

export const initialMail = () => ({skills: {email: 'configurar'}, reply: false, setup: null, drafts: []});

const TERMINAL = new Set(['enviada', 'falhou', 'desconhecida', 'descartada', 'expirada']);
export const isOpen = draft => draft.state === 'rascunho';
export const isFinal = draft => TERMINAL.has(draft.state);

const upsert = (drafts, draft) => {
  const next = drafts.some(item => item.id === draft.id) ? drafts.map(item => item.id === draft.id ? {...item, ...draft} : item) : [...drafts, draft];
  return next.slice(-20);
};

export function applyMailEvent(state, event) {
  switch (event.type) {
    case 'session':
      return {...state, skills: {...state.skills, email: event.mail === 'real' ? 'ativa' : 'configurar'}, reply: Boolean(event.reply)};
    case 'setup':
      return {...state, setup: {busy: false, error: '', done: ''}};
    case 'setup_result':
      if (!event.ok) return {...state, setup: {busy: false, error: event.message || 'Não foi possível configurar.', done: ''}};
      return {...state, skills: {...state.skills, email: 'ativa'}, reply: Boolean(event.reply), setup: {busy: false, error: '', done: event.message || 'Caixa conectada.'}};
    case 'skill':
      return {...state, skills: {...state.skills, [event.id]: event.state}};
    case 'status':
      if (event.state !== 'ready') return state;
      return {...state, skills: Object.fromEntries(Object.entries(state.skills).map(([id, value]) => [id, value === 'usando' ? 'ativa' : value]))};
    case 'draft':
      return event.draft?.id ? {...state, drafts: upsert(state.drafts, event.draft)} : state;
    case 'drafts':
      return {...state, drafts: (event.drafts || []).filter(item => item?.id).slice(-20)};
    case 'draft_result':
      return event.draft?.id ? {...state, drafts: upsert(state.drafts, event.draft)} : state;
    default:
      return state;
  }
}

export const closeSetup = state => ({...state, setup: null});
export const startSetup = state => state.setup ? {...state, setup: {...state.setup, busy: true, error: '', done: ''}} : state;

/** The message the form sends. Only fields the person typed; empty advanced fields are left out so the server guesses them. */
export function setupMessage({email, password, imapHost, smtpHost, readOnly}) {
  const message = {type: 'setup_mail', email: (email || '').trim(), password: password || ''};
  if ((imapHost || '').trim()) message.imap_host = imapHost.trim();
  if ((smtpHost || '').trim()) message.smtp_host = smtpHost.trim();
  if (readOnly) message.read_only = true;
  return message;
}
export const validSetup = ({email, password}) => /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/.test((email || '').trim()) && (password || '').trim().length >= 4;

/** The decision for a draft: it carries the hash that was shown, so a changed draft can never be approved by that click. */
export const decisionMessage = (draft, decision) => decision === 'approve'
  ? {type: 'draft_decision', id: draft.id, decision, hash: draft.hash} : {type: 'draft_decision', id: draft.id, decision};

export const DRAFT_LABEL = {rascunho: 'Aguardando sua aprovação', enviando: 'Enviando…', enviada: 'Enviada', falhou: 'Não enviada',
  desconhecida: 'Resultado desconhecido', descartada: 'Descartada', expirada: 'Expirada'};
export const SKILL_LABEL = {configurar: 'E-mail · configurar', ativa: 'E-mail · ativa', usando: 'E-mail · em uso'};

/** Domain hint shown while typing; the server decides the real servers. */
export function providerHint(email) {
  const domain = (email.split('@')[1] || '').toLowerCase();
  if (['gmail.com', 'googlemail.com'].includes(domain)) return 'Gmail: ative a verificação em duas etapas e crie uma senha de app em Conta Google › Segurança.';
  if (['outlook.com', 'hotmail.com', 'live.com'].includes(domain)) return 'Outlook: crie uma senha de app na segurança da conta Microsoft. Contas de trabalho podem bloquear esse acesso.';
  if (['yahoo.com', 'yahoo.com.br'].includes(domain)) return 'Yahoo: gere uma senha de app em Segurança da conta.';
  if (['icloud.com', 'me.com'].includes(domain)) return 'iCloud: gere uma senha específica de app na página da sua conta Apple.';
  return domain ? 'Use uma senha de aplicativo, não a senha da conta. Em Avançado você pode ajustar os servidores.' : '';
}
