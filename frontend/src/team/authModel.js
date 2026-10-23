// Pure rules for signing in and for what a role may see. The server decides everything; this only hides what would be refused.

export const ROLES = ['leitor', 'atendente', 'aprovador', 'admin'];
export const ROLE_LABEL = {leitor: 'Leitor', atendente: 'Atendente', aprovador: 'Aprovador', admin: 'Administrador', owner: 'Dono local'};
export const ROLE_HELP = {
  leitor: 'Só vê demandas, equipe e lacunas.', atendente: 'Abre e conduz demandas; não aprova.',
  aprovador: 'Também decide aprovações e confere resultados desconhecidos.', admin: 'Configura a empresa e gerencia membros.',
};

/** Whether a role reaches at least `minimum`. The local owner (role "owner" or none) is not limited by roles. */
export function canAt(role, minimum) {
  if (!role || role === 'owner') return true;
  return ROLES.indexOf(role) >= ROLES.indexOf(minimum);
}

const LOGIN_ERRORS = {
  nao_configurado: 'O login com Google ainda não foi configurado neste computador.',
  cancelado: 'O login foi cancelado.',
  estado_invalido: 'O login expirou ou foi aberto em outro navegador. Tente entrar de novo.',
  troca_recusada: 'O Google recusou a conclusão do login. Tente de novo.',
  provedor_indisponivel: 'Não foi possível falar com o Google agora. Tente em instantes.',
  identidade_invalida: 'A identidade devolvida pelo Google não pôde ser confirmada.',
  email_nao_verificado: 'O email dessa conta Google não está verificado.',
  dominio_nao_permitido: 'Essa conta Google não pertence ao domínio permitido.',
  conta_nao_autorizada: 'Essa conta não tem acesso. Peça a um administrador para cadastrar o seu email.',
  limite: 'Muitas tentativas. Aguarde um minuto e tente de novo.',
};
export const loginMessage = code => LOGIN_ERRORS[code] || '';

/** The error code the server put in the address after a failed sign-in (only known codes are ever shown). */
export function loginErrorFrom(search) {
  const code = new URLSearchParams(search || '').get('login_error');
  return code && Object.hasOwn(LOGIN_ERRORS, code) ? code : '';
}

/** Which tabs a role sees. Members is for administrators (and the local owner). */
export function visibleTabs(tabs, role) {
  const needs = {cases: 'leitor', departments: 'admin', agents: 'admin', skills: 'admin', connections: 'admin', members: 'admin'};
  return tabs.filter(([id]) => canAt(role, needs[id] || 'admin'));
}

export const sessionKind = (status, data) => status === 200 && data?.authenticated ? (data.mode === 'remote' ? 'remote' : 'local')
  : data?.mode === 'remote' ? 'login' : status === 200 ? 'local' : 'unreachable';
