import test from 'node:test';
import assert from 'node:assert/strict';
import {applyMailEvent, closeSetup, decisionMessage, initialMail, isFinal, isOpen, providerHint, setupMessage, startSetup, validSetup} from '../src/mailModel.js';

const draft = (extra = {}) => ({id: 'd1', state: 'rascunho', to: 'bruno@exemplo.com', subject: 'Re: Reuniao', body: 'Combinado.', hash: 'h1', ...extra});
const run = events => events.reduce(applyMailEvent, initialMail());

test('the skill starts unconfigured and the session tells whether a real mailbox and sending are set up', () => {
  assert.equal(initialMail().skills.email, 'configurar');
  const state = run([{type: 'session', mail: 'real', reply: true}]);
  assert.equal(state.skills.email, 'ativa');
  assert.equal(state.reply, true);
  assert.equal(run([{type: 'session', mail: 'demo', reply: false}]).skills.email, 'configurar');
});

test('the setup form opens, shows a failure without closing, and a success activates the skill', () => {
  let state = run([{type: 'setup', skill: 'email'}]);
  assert.deepEqual(state.setup, {busy: false, error: '', done: ''});
  state = startSetup(state);
  assert.equal(state.setup.busy, true);
  state = applyMailEvent(state, {type: 'setup_result', ok: false, message: 'O servidor recusou a senha.'});
  assert.equal(state.setup.busy, false);
  assert.equal(state.setup.error, 'O servidor recusou a senha.');
  assert.equal(state.skills.email, 'configurar');
  state = applyMailEvent(startSetup(state), {type: 'setup_result', ok: true, reply: true, message: 'Caixa conectada.'});
  assert.equal(state.skills.email, 'ativa');
  assert.equal(state.reply, true);
  assert.equal(state.setup.done, 'Caixa conectada.');
  assert.equal(closeSetup(state).setup, null);
});

test('the setup message carries only what was typed and never an empty advanced field', () => {
  assert.deepEqual(setupMessage({email: ' ana@gmail.com ', password: 'abcd efgh', imapHost: '', smtpHost: ' ', readOnly: false}),
    {type: 'setup_mail', email: 'ana@gmail.com', password: 'abcd efgh'});
  const full = setupMessage({email: 'a@b.co', password: 'xxxx', imapHost: 'imap.b.co', smtpHost: 'smtp.b.co', readOnly: true});
  assert.equal(full.imap_host, 'imap.b.co');
  assert.equal(full.read_only, true);
  assert.equal(validSetup({email: 'sem-arroba', password: 'xxxx'}), false);
  assert.equal(validSetup({email: 'a@b.co', password: 'abc'}), false);
  assert.equal(validSetup({email: 'a@b.co', password: 'abcd'}), true);
});

test('a skill in use goes back to active when the turn ends, and an unconfigured one is never promoted', () => {
  let state = run([{type: 'session', mail: 'real'}, {type: 'skill', id: 'email', state: 'usando'}]);
  assert.equal(state.skills.email, 'usando');
  state = applyMailEvent(state, {type: 'status', state: 'processing'});
  assert.equal(state.skills.email, 'usando');
  state = applyMailEvent(state, {type: 'status', state: 'ready'});
  assert.equal(state.skills.email, 'ativa');
  assert.equal(applyMailEvent(initialMail(), {type: 'status', state: 'ready'}).skills.email, 'configurar');
});

test('drafts arrive, update by id, come back after a reload and the approval carries the hash that was shown', () => {
  let state = run([{type: 'draft', draft: draft()}]);
  assert.equal(state.drafts.length, 1);
  assert.ok(isOpen(state.drafts[0]));
  assert.deepEqual(decisionMessage(state.drafts[0], 'approve'), {type: 'draft_decision', id: 'd1', decision: 'approve', hash: 'h1'});
  assert.deepEqual(decisionMessage(state.drafts[0], 'discard'), {type: 'draft_decision', id: 'd1', decision: 'discard'});
  state = applyMailEvent(state, {type: 'draft_result', draft: {id: 'd1', state: 'enviando'}});
  assert.equal(state.drafts[0].state, 'enviando');
  assert.equal(state.drafts[0].body, 'Combinado.');  // the update keeps what the person was shown
  state = applyMailEvent(state, {type: 'draft_result', draft: {id: 'd1', state: 'enviada', detail: 'Aceita.'}});
  assert.ok(isFinal(state.drafts[0]));
  state = applyMailEvent(state, {type: 'drafts', drafts: [draft({id: 'd2'}), {nope: 1}]});
  assert.deepEqual(state.drafts.map(item => item.id), ['d2']);
});

test('malformed events change nothing and the list is bounded', () => {
  const state = run([{type: 'draft'}, {type: 'draft', draft: {}}, {type: 'draft_result'}, {type: 'qualquer'}]);
  assert.deepEqual(state, initialMail());
  const many = run(Array.from({length: 30}, (_, i) => ({type: 'draft', draft: draft({id: `d${i}`})})));
  assert.equal(many.drafts.length, 20);
});

test('provider hints never claim more than the form can check', () => {
  assert.match(providerHint('a@gmail.com'), /senha de app/);
  assert.match(providerHint('a@empresa.com.br'), /senha de aplicativo/);
  assert.equal(providerHint('sem'), '');
});
