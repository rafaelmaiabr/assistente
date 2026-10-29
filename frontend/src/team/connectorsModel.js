// Pure rules for the connectors tab. The server decides everything (descriptor fields, validation, vault); this only builds
// the form from the driver's declared fields and words the real state. Nothing here keeps or logs a secret.

/** Empty form values for a driver's fields (a secret field is always empty: it is never sent back by the server). */
export const blankValues = fields => Object.fromEntries((fields || []).map(f => [f.name, '']));

/** Form values for editing: what the server stored (never a secret) over the blank form. */
export const editValues = (fields, stored) => ({...blankValues(fields), ...Object.fromEntries(Object.entries(stored || {}).map(([k, v]) => [k, String(v)]))});

const isBlank = value => value === undefined || value === null || String(value).trim() === '';

/** The exact `values` body for the API: blank fields are left out, numbers are numbers, and a blank secret means "keep". */
export function formPayload(fields, values) {
  const body = {};
  for (const field of fields || []) {
    const raw = values[field.name];
    if (isBlank(raw)) continue;
    body[field.name] = field.kind === 'number' ? Number(String(raw).trim()) : field.kind === 'secret' ? String(raw) : String(raw).trim();
  }
  return body;
}

/** A sentence for the first problem found, or '' when the form can be sent. `editing` allows keeping the current secret. */
export function validateForm(fields, values, {editing = false} = {}) {
  for (const field of fields || []) {
    const raw = values[field.name];
    if (isBlank(raw)) {
      if (field.kind === 'secret' ? !editing && field.required !== false : field.required !== false) return `Preencha o campo: ${field.label}.`;
      continue;
    }
    if (field.kind === 'number' && !/^\d{1,5}$/.test(String(raw).trim())) return `${field.label} deve ser um número.`;
    if (field.kind === 'url' && !/^https?:\/\/[^\s/@]+/.test(String(raw).trim())) return `${field.label} deve ser um endereço http(s).`;
    if (field.kind === 'secret' && String(raw).length > 500) return `${field.label} é longo demais.`;
  }
  return '';
}

const CODES = {
  ok: 'Conexão testada com sucesso.', autenticacao_recusada: 'O servidor recusou o usuário ou a senha (use senha de aplicativo, se o provedor exigir).',
  tls_invalido: 'Falha de TLS ou certificado não aceito: confira servidor e porta.', sem_conexao: 'Não foi possível conectar: confira servidor, porta e rede.',
  tempo_esgotado: 'O servidor demorou demais para responder.', conta_nao_configurada: 'Informe servidor, usuário e as demais configurações.',
  pasta_nao_autorizada: 'Uma das pastas não está autorizada.', sem_credencial: 'Esta conexão ainda não tem senha salva no cofre.',
};
export const codeLabel = code => CODES[code] || (code ? 'O teste falhou. Confira as configurações.' : '');

/** The badge for a connection: only what was really tested is claimed; otherwise it says it was not tested. A driver that
 * needs no credential (`auth: 'none'`) is never "without credential". */
export function connectionState(connection, auth = 'secret') {
  if (!connection.configured) return {text: 'Não configurada', tone: 'off'};
  if (auth === 'none') {
    if (connection.status !== 'ativo') return {text: 'Desativada', tone: 'off'};
    if (!connection.last_test) return {text: 'Não testada', tone: 'warn'};
    return connection.last_test.ok ? {text: 'Testada: funcionou', tone: 'ok'} : {text: 'Teste falhou', tone: 'off'};
  }
  if (!connection.credential) return {text: 'Sem credencial', tone: 'off'};
  if (connection.credential.state !== 'ativa') return {text: 'Credencial revogada', tone: 'off'};
  if (connection.status !== 'ativo') return {text: 'Desativada', tone: 'off'};
  if (!connection.last_test) return {text: 'Não testada', tone: 'warn'};
  return connection.last_test.ok ? {text: 'Testada: funcionou', tone: 'ok'} : {text: 'Teste falhou', tone: 'off'};
}

/** When the last test ran, in the viewer's language; empty when there was none. */
export function lastTestLine(connection) {
  const test = connection.last_test;
  if (!test) return 'Ainda não foi feito nenhum teste.';
  const when = new Date(test.at);
  const stamp = Number.isNaN(when.getTime()) ? test.at : when.toLocaleString('pt-BR');
  return `Último teste em ${stamp}: ${test.ok ? 'funcionou' : codeLabel(test.code)}`;
}

/** The warning shown for a driver that does not use MCP (MCP is the preferred protocol); '' for MCP drivers. */
export const driverNotice = driver => driver.mcp ? '' : `Este conector não usa MCP: ${driver.non_mcp_justification}`;

const ACTIONS = {conectar: 'Conexão criada', configurar: 'Configuração salva', testar: 'Teste'};
export const auditLine = entry => `${new Date(entry.at).toLocaleString('pt-BR')} · ${ACTIONS[entry.action] || entry.action} · ${entry.provider} · ${entry.ok ? 'ok' : codeLabel(entry.code) || 'falhou'}`;
