import test from 'node:test';
import assert from 'node:assert/strict';
import {auditLine, blankValues, codeLabel, connectionState, driverNotice, editValues, formPayload, lastTestLine, validateForm} from '../src/team/connectorsModel.js';
import {visibleTabs} from '../src/team/authModel.js';

const fields = [
  {name: 'servidor', label: 'Servidor IMAP', kind: 'text', required: true},
  {name: 'porta', label: 'Porta', kind: 'number', required: false},
  {name: 'pastas', label: 'Pastas autorizadas', kind: 'text', required: true},
  {name: 'senha', label: 'Senha de aplicativo', kind: 'secret', required: true},
];

test('the form is built from the driver fields and a secret starts empty, also when editing', () => {
  assert.deepEqual(blankValues(fields), {servidor: '', porta: '', pastas: '', senha: ''});
  assert.deepEqual(editValues(fields, {servidor: 'imap.exemplo.com', porta: 993, pastas: 'INBOX'}), {servidor: 'imap.exemplo.com', porta: '993', pastas: 'INBOX', senha: ''});
  assert.equal(editValues(fields, null).senha, '');
});

test('the payload leaves blanks out, sends numbers as numbers and keeps the secret as typed', () => {
  assert.deepEqual(formPayload(fields, {servidor: ' imap.exemplo.com ', porta: ' 993 ', pastas: 'INBOX', senha: ' s3 '}),
    {servidor: 'imap.exemplo.com', porta: 993, pastas: 'INBOX', senha: ' s3 '});
  assert.deepEqual(formPayload(fields, {servidor: 'x', porta: '', pastas: 'INBOX', senha: ''}), {servidor: 'x', pastas: 'INBOX'});  // a blank secret means "keep"
});

test('validation names the first problem; editing may leave the secret blank', () => {
  const ok = {servidor: 'imap.exemplo.com', porta: '993', pastas: 'INBOX', senha: 'x'};
  assert.equal(validateForm(fields, ok), '');
  assert.equal(validateForm(fields, {...ok, servidor: ' '}), 'Preencha o campo: Servidor IMAP.');
  assert.equal(validateForm(fields, {...ok, senha: ''}), 'Preencha o campo: Senha de aplicativo.');
  assert.equal(validateForm(fields, {...ok, senha: ''}, {editing: true}), '');
  assert.equal(validateForm(fields, {...ok, porta: '99a'}), 'Porta deve ser um número.');
  assert.equal(validateForm(fields, {...ok, porta: ''}), '');  // optional
  assert.match(validateForm([{name: 'u', label: 'Endereço', kind: 'url', required: true}], {u: 'ftp://x'}), /http\(s\)/);
  assert.match(validateForm(fields, {...ok, senha: 'x'.repeat(501)}), /longo demais/);
});

test('a connection never claims more than what was really done', () => {
  const base = {configured: true, credential: {state: 'ativa'}, status: 'ativo', last_test: null};
  assert.deepEqual(connectionState({...base, configured: false}), {text: 'Não configurada', tone: 'off'});
  assert.equal(connectionState({...base, credential: null}).text, 'Sem credencial');
  assert.equal(connectionState({...base, credential: {state: 'revogada'}}).text, 'Credencial revogada');
  assert.equal(connectionState({...base, status: 'inativo'}).text, 'Desativada');
  assert.deepEqual(connectionState(base), {text: 'Não testada', tone: 'warn'});  // configured is not the same as working
  assert.equal(connectionState({...base, last_test: {ok: true}}).text, 'Testada: funcionou');
  assert.equal(connectionState({...base, last_test: {ok: false}}).text, 'Teste falhou');
});

test('test lines and codes are words, with a safe default for unknown codes', () => {
  assert.equal(lastTestLine({last_test: null}), 'Ainda não foi feito nenhum teste.');
  assert.match(lastTestLine({last_test: {at: '2026-11-01T10:00:00Z', ok: true}}), /funcionou/);
  assert.match(lastTestLine({last_test: {at: '2026-11-01T10:00:00Z', ok: false, code: 'autenticacao_recusada'}}), /senha de aplicativo/);
  assert.match(lastTestLine({last_test: {at: 'ontem', ok: false, code: 'xyz'}}), /ontem.*falhou/);
  assert.equal(codeLabel('sem_conexao').startsWith('Não foi possível conectar'), true);
  assert.equal(codeLabel(null), '');
  assert.equal(codeLabel('algo_novo'), 'O teste falhou. Confira as configurações.');
});

test('a driver that is not MCP says why; an MCP driver shows no warning', () => {
  assert.equal(driverNotice({mcp: true}), '');
  assert.equal(driverNotice({mcp: false, non_mcp_justification: 'O envio sai pelo serviço de saída.'}), 'Este conector não usa MCP: O envio sai pelo serviço de saída.');
});

test('audit lines carry the action and the result, never values', () => {
  const line = auditLine({at: '2026-11-01T10:00:00Z', action: 'testar', provider: 'imap', ok: false, code: 'sem_conexao'});
  assert.match(line, /Teste · imap · Não foi possível conectar/);
  assert.match(auditLine({at: '2026-11-01T10:00:00Z', action: 'conectar', provider: 'smtp', ok: true}), /Conexão criada · smtp · ok/);
});

test('only administrators see the connectors tab', () => {
  const tabs = [['cases', 'Demandas'], ['connectors', 'Conectores']];
  assert.deepEqual(visibleTabs(tabs, 'atendente').map(t => t[0]), ['cases']);
  assert.deepEqual(visibleTabs(tabs, 'admin').map(t => t[0]), ['cases', 'connectors']);
  assert.deepEqual(visibleTabs(tabs, 'owner').map(t => t[0]), ['cases', 'connectors']);
});
