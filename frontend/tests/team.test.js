import test from 'node:test';
import assert from 'node:assert/strict';
import {agentDraft, agentPayload, blankAgent, changedFields, commaList, credentialLabel, describeError, joinLines, lines,
  operationsLabel, permissionLines, secretIsAcceptable, validateAgent} from '../src/team/model.js';
import {ApiError, request} from '../src/team/api.js';

const agent = {department_id: 'dep_1', name: 'Financeiro', goals: ['Cobrar', 'Resumir'], personality: {tone: 'claro', initiative: 'proativo'},
  scope: 'Cobrancas', out_of_scope: 'Outros', routing_policy: 'encaminhar', model_policy_ref: 'local-primeiro',
  voice_profile_ref: 'pf_dora', handoff_target_ids: ['agt_2'], capabilities: ['financeiro'], status: 'ativo'};

test('text lists are trimmed and empty lines dropped', () => {
  assert.deepEqual(lines(' a \n\n b\n'), ['a', 'b']);
  assert.equal(joinLines(['a', 'b']), 'a\nb');
  assert.deepEqual(commaList('x, y,, z '), ['x', 'y', 'z']);
  assert.deepEqual(lines(undefined), []);
});

test('a form round-trips to the exact API payload', () => {
  const draft = agentDraft(agent);
  assert.deepEqual(agentPayload(draft), {department_id: 'dep_1', name: 'Financeiro', goals: ['Cobrar', 'Resumir'],
    personality: {tone: 'claro', initiative: 'proativo'}, scope: 'Cobrancas', out_of_scope: 'Outros', routing_policy: 'encaminhar',
    model_policy_ref: 'local-primeiro', voice_profile_ref: 'pf_dora', handoff_target_ids: ['agt_2'], capabilities: ['financeiro']});
  assert.equal(agentPayload({...draft, voice_profile_ref: ''}).voice_profile_ref, null);
  assert.equal(agentPayload({...draft, name: '  Nome  '}).name, 'Nome');
});

test('required fields are reported and a complete draft is accepted', () => {
  const blank = blankAgent('dep_1');
  const problems = validateAgent(blank);
  assert.ok(problems.some(p => p.startsWith('Nome')) && problems.some(p => p.startsWith('Escopo')));
  assert.deepEqual(validateAgent(agentDraft(agent)), []);
  assert.ok(validateAgent({...agentDraft(agent), name: 'x'.repeat(121)}).length > 0);
});

test('unsaved changes list only what differs', () => {
  const draft = agentDraft(agent);
  assert.deepEqual(changedFields(agent, draft), []);
  assert.deepEqual(changedFields(agent, {...draft, scope: 'Novo', handoff_target_ids: []}).sort(), ['handoff_target_ids', 'scope']);
});

test('API errors become short sentences without internals', () => {
  assert.match(describeError(new ApiError('x', 409, 'conflict')), /Recarregue/);
  assert.match(describeError(new ApiError('x', 409, 'persistence_disabled')), /atendimento persistente/);
  assert.match(describeError(new ApiError('x', 404)), /Não encontrado/);
  assert.equal(describeError(new ApiError('Perfil de voz inexistente.', 400)), 'Perfil de voz inexistente.');
  assert.match(describeError(new ApiError('Dados invalidos.', 400, undefined, ['a', 'b', 'c', 'd'])), /Dados invalidos\. a; b; c$/);
  assert.equal(describeError(null), '');
});

test('permissions and credentials are described without values', () => {
  assert.deepEqual(permissionLines({permissions: [{tool: 'erp.listar', operations: ['read']}]}), ['erp.listar — read']);
  assert.match(permissionLines({permissions: []})[0], /somente instruções/);
  assert.equal(operationsLabel(['read', 'spend']), 'leitura, pagamento');
  assert.equal(credentialLabel({credential: null}), 'Sem credencial');
  assert.match(credentialLabel({credential: {state: 'ativa', store: 'cofre-local'}}), /cofre do Windows/);
  assert.equal(credentialLabel({credential: {state: 'revogada', store: 'cofre-local'}}), 'Credencial revogada');
});

test('secrets must be single-line and trimmed before they are sent', () => {
  assert.equal(secretIsAcceptable('abc123'), true);
  for (const bad of ['', ' padded', 'padded ', 'two\nlines', 'x'.repeat(4097), null, 5]) assert.equal(secretIsAcceptable(bad), false, String(bad));
});

test('requests send JSON to /api/team and failures keep status, code and details only', async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    if (url.endsWith('/boom')) return {ok: false, status: 400, json: async () => ({error: 'Dados invalidos.', code: 'x', details: ['d']})};
    if (url.endsWith('/html')) return {ok: false, status: 502, json: async () => {throw new Error('not json');}};
    return {ok: true, status: 200, json: async () => ({ok: true})};
  };
  assert.deepEqual(await request('POST', '/companies', {name: 'A'}), {ok: true});
  assert.equal(calls[0].url, '/api/team/companies');
  assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
  assert.equal(calls[0].options.body, '{"name":"A"}');
  assert.equal(calls[0].options.method, 'POST');
  await assert.rejects(request('GET', '/boom'), error => error instanceof ApiError && error.status === 400 && error.code === 'x' && error.details[0] === 'd');
  await assert.rejects(request('GET', '/html'), error => error.status === 502 && /Não foi possível/.test(error.message));
  globalThis.fetch = async () => {throw new TypeError('network');};
  await assert.rejects(request('GET', '/x'), error => error.status === 0 && /Sem conexão/.test(error.message));
});
