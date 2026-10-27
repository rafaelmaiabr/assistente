import test from 'node:test';
import assert from 'node:assert/strict';
import {applyIntegrationEvent, cancelMessage, cardKey, cardNotice, initialIntegrations, isIntegrationEvent, oauthMessage, openMessage, pickMessage,
  resumeText, rowActions, rowText, safeAuthUrl, shouldResume, submitMessage, useMessage} from '../src/integrationModel.js';

const card = (id = 'a1', extra = {}) => ({request_id: id, state: 'pronto', provider: 'imap', label: 'Email (leitura, IMAP)', rows: [], ...extra});
const fields = [{name: 'servidor', label: 'Servidor', kind: 'text', required: true}, {name: 'porta', label: 'Porta', kind: 'number', required: false},
  {name: 'senha', label: 'Senha', kind: 'secret', required: true}];

test('cards are kept by request id, newest last, only a few', () => {
  let state = initialIntegrations();
  for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) state = applyIntegrationEvent(state, {type: 'integration_card', card: card(id)});
  assert.deepEqual(state.cards.map(cardKey), ['b', 'c', 'd', 'e', 'f']);
  state = applyIntegrationEvent(state, {type: 'integration_card', card: card('c', {state: 'escolher', options: []})});
  assert.deepEqual(state.cards.map(cardKey), ['b', 'd', 'e', 'f', 'c']);
  assert.equal(state.cards.at(-1).state, 'escolher');
  assert.equal(applyIntegrationEvent(state, {type: 'integration_card', card: null}), state);
  assert.equal(applyIntegrationEvent(state, {type: 'integration_card', card: {request_id: 'x'}}), state);
});

test('the card for a system that needs no company has a stable key', () => {
  const off = {state: 'persistencia_desligada', request_id: null};
  assert.equal(cardKey(off), 'persistencia');
  const state = applyIntegrationEvent(initialIntegrations(), {type: 'integration_card', card: off});
  assert.equal(state.cards.length, 1);
});

test('a form, its busy state and its result follow the events of that card only', () => {
  let state = applyIntegrationEvent(initialIntegrations(), {type: 'integration_card', card: card('a')});
  state = applyIntegrationEvent(state, {type: 'integration_card', card: card('b')});
  state = applyIntegrationEvent(state, {type: 'integration_form', form: {request_id: 'a', fields, company_id: 'ten_1'}});
  assert.ok(state.forms.a && !state.forms.b);
  state = applyIntegrationEvent(state, {type: 'integration_busy', request_id: 'a', text: 'Testando…'});
  assert.deepEqual(state.status.a, {busy: true, text: 'Testando…'});
  const failed = applyIntegrationEvent(state, {type: 'integration_result', request_id: 'a', ok: false, message: 'O servidor recusou.'});
  assert.ok(failed.forms.a, 'a failed attempt keeps the form so the person can fix it');
  assert.deepEqual(failed.status.a, {busy: false, ok: false, message: 'O servidor recusou.', next: null, resume: false});
  const done = applyIntegrationEvent(state, {type: 'integration_result', request_id: 'a', ok: true, message: 'Conectado.', resume: true});
  assert.equal(done.forms.a, undefined, 'a connected system closes its form');
  assert.equal(shouldResume(done.status.a), true);
  assert.equal(done.status.b, undefined);
});

test('a result that still needs the browser keeps the form and does not resume yet', () => {
  let state = applyIntegrationEvent(initialIntegrations(), {type: 'integration_card', card: card('a')});
  state = applyIntegrationEvent(state, {type: 'integration_form', form: {request_id: 'a', fields: [], company_id: 'ten_1', auth: 'oauth'}});
  state = applyIntegrationEvent(state, {type: 'integration_result', request_id: 'a', ok: true, next: 'oauth', resume: true, message: 'Falta autorizar.'});
  assert.ok(state.forms.a);
  assert.equal(shouldResume(state.status.a), false);
  state = applyIntegrationEvent(state, {type: 'integration_oauth', request_id: 'a', url: 'https://login.exemplo.com/auth?state=1'});
  assert.deepEqual([state.status.a.waiting, state.status.a.url], [true, 'https://login.exemplo.com/auth?state=1']);
  const unsafe = applyIntegrationEvent(state, {type: 'integration_oauth', request_id: 'a', url: 'javascript:alert(1)'});
  assert.equal(unsafe.status.a.url, '');
});

test('errors without a card are kept apart and never throw', () => {
  const state = applyIntegrationEvent(initialIntegrations(), {type: 'integration_error', request_id: null, message: 'Este cartão expirou.'});
  assert.equal(state.status.geral.message, 'Este cartão expirou.');
  assert.equal(applyIntegrationEvent(state, {type: 'message'}), state);
});

test('only integration events are claimed by the integration cards', async () => {
  const {isIntegrationEvent} = await import('../src/integrationModel.js');
  assert.equal(isIntegrationEvent({type: 'integration_card'}), true);
  assert.equal(isIntegrationEvent({type: 'message'}), false);
  assert.equal(isIntegrationEvent(null), false);
});

test('only https sign-in pages without credentials are opened', () => {
  assert.equal(safeAuthUrl('https://login.exemplo.com/authorize?x=1'), true);
  assert.equal(safeAuthUrl('http://127.0.0.1:8765/oauth'), true);
  for (const bad of ['http://login.exemplo.com/x', 'javascript:alert(1)', 'https://user:pw@login.exemplo.com/', 'file:///c:/x', 'ftp://x/y', '', null, undefined, 'https://' + 'a'.repeat(2001)]) {
    assert.equal(safeAuthUrl(bad), false, String(bad));
  }
});

test('each row offers exactly the actions its state allows', () => {
  assert.deepEqual(rowActions({state: 'nova'}).map(a => a.id), ['connect']);
  assert.deepEqual(rowActions({state: 'existente'}).map(a => a.id), ['use', 'reconfigure']);
  assert.deepEqual(rowActions({state: 'reconectar'}).map(a => a.id), ['reconnect']);
  assert.equal(rowText({state: 'nova'}), 'Ainda não conectado.');
  assert.equal(rowText({state: 'existente', connection: {label: 'Caixa'}}), 'Já conectado: Caixa.');
  assert.equal(rowText({state: 'reconectar', connection: {label: 'Caixa'}, reason: 'teve a credencial revogada'}), 'A conexão “Caixa” teve a credencial revogada.');
});

test('cards without rows say what happened, never promise a connection', () => {
  assert.match(cardNotice({state: 'indisponivel', wanted: 'Bling', available: ['Email (leitura, IMAP)']}), /Não há um conector instalado para “Bling”.*Email \(leitura, IMAP\)/);
  assert.match(cardNotice({state: 'indisponivel'}), /esse sistema/);
  assert.equal(cardNotice({state: 'escolher'}), 'Qual destes você quer conectar?');
  assert.match(cardNotice({state: 'sem_empresa'}), /Crie uma empresa/);
  assert.match(cardNotice({state: 'persistencia_desligada'}), /atendimento persistente/);
  assert.equal(cardNotice({state: 'pronto'}), '');
});

test('clicks become exactly the messages the server expects, and the secret travels only in submit', () => {
  const c = {request_id: 'a1'}, row = {company_id: 'ten_1'};
  assert.deepEqual(openMessage(c, row), {type: 'integration_open', request_id: 'a1', company_id: 'ten_1'});
  assert.deepEqual(useMessage(c, row), {type: 'integration_use', request_id: 'a1', company_id: 'ten_1'});
  assert.deepEqual(pickMessage(c, 'smtp'), {type: 'integration_pick', request_id: 'a1', provider: 'smtp'});
  const form = {request_id: 'a1', company_id: 'ten_1', label: 'Email', editing: false, fields};
  assert.deepEqual(oauthMessage(form), {type: 'integration_oauth', request_id: 'a1', company_id: 'ten_1'});
  assert.deepEqual(cancelMessage('a1'), {type: 'integration_cancel', request_id: 'a1'});
  assert.deepEqual(submitMessage(form, '  Minha caixa ', {servidor: ' imap.exemplo.com ', porta: '993', senha: 's3'}),
    {type: 'integration_submit', request_id: 'a1', company_id: 'ten_1', label: 'Minha caixa', values: {servidor: 'imap.exemplo.com', porta: 993, senha: 's3'}});
  const editing = submitMessage({...form, editing: true}, 'ignorado', {servidor: 'x', porta: '', senha: ''});
  assert.deepEqual(editing, {type: 'integration_submit', request_id: 'a1', company_id: 'ten_1', values: {servidor: 'x'}});  // blank secret keeps the current one
  assert.equal('label' in editing, false);
  assert.equal(submitMessage(form, '', {servidor: 'x', senha: 's'}).label, 'Email');
});

test('the sentence sent after connecting names the system and resumes the earlier request', () => {
  assert.equal(resumeText('Email (leitura, IMAP)'), 'Conclui a conexão com Email (leitura, IMAP). Pode continuar com o que eu pedi.');
  assert.equal(shouldResume({ok: true, resume: true, next: null}), true);
  assert.equal(shouldResume({ok: true, resume: false}), false);
  assert.equal(shouldResume({ok: false, resume: true}), false);
  assert.equal(shouldResume(undefined), false);
});
