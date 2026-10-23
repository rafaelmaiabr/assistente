import test from 'node:test';
import assert from 'node:assert/strict';
import {applyEvent, avatarStatus, bindingLabel, controls, foreignBinding, initialConversation, isCaseRef, median, reattachMessage,
  reconnected, select, startTurn} from '../src/conversationModel.js';

const CO = 'ten_' + '1'.repeat(32), CASE_A = 'cas_' + 'a'.repeat(32), CASE_B = 'cas_' + 'b'.repeat(32);
const binding = (caseId = CASE_A, agent = 'Suporte', extra = {}) => ({mode: 'equipe', company: {id: CO, name: 'Empresa A'},
  case: {id: caseId, label: 'Site fora do ar', phase: 'andamento'}, agent: {id: 'agt_x', name: agent}, waiting_human: false, paused: false, ...extra});
const compact = (caseId = CASE_A) => ({mode: 'equipe', company_id: CO, case_id: caseId, agent_id: 'agt_x', agent_version: 1});
const run = (state, events, now = 0) => events.reduce((acc, e) => {const r = applyEvent(acc.state, e, now); return {state: r.state, log: [...acc.log, {e, ...r}]};}, {state, log: []});

test('the binding of a turn is captured when it starts and survives a later announcement', () => {
  let state = applyEvent(initialConversation(), {type: 'binding', binding: binding(), reason: 'vinculo'}).state;
  state = startTurn(state, 100);
  assert.equal(state.turnBinding.case.id, CASE_A);
  state = applyEvent(state, {type: 'binding', binding: binding(CASE_B), reason: 'mudou'}).state;
  assert.equal(state.turnBinding.case.id, CASE_A);  // the running turn keeps what it started with
  assert.equal(state.binding.case.id, CASE_B);      // the next one will use the new one
});

test('late events of an earlier turn are dropped, and so are events of a cancelled turn', () => {
  let state = startTurn(applyEvent(initialConversation(), {type: 'binding', binding: binding()}).state, 0);
  const {state: after, log} = run(state, [
    {type: 'status', state: 'processing', turn: 2}, {type: 'status', state: 'ready', turn: 1},  // turn 1 ends after turn 2 began
    {type: 'message', role: 'assistant', text: 'tarde', turn: 1, binding: compact()}, {type: 'message', role: 'assistant', text: 'certa', turn: 2, binding: compact()}]);
  assert.deepEqual(log.map(l => l.accept), [true, false, false, true]);
  assert.equal(after.turn, 2);
  assert.equal(after.running, true);  // the stale "ready" did not end the turn that is running
  const cancelled = run(after, [{type: 'cancelled'}, {type: 'message', role: 'assistant', text: 'depois do cancelamento', turn: 2, binding: compact()}]);
  assert.deepEqual(cancelled.log.map(l => l.accept), [true, false]);
  assert.equal(cancelled.state.running, false);
});

test('an answer or a voice from another case or company never reaches the active conversation', () => {
  const state = {...initialConversation(), binding: binding(), turn: 1};
  const message = {type: 'message', role: 'assistant', text: 'x', turn: 1, binding: compact(CASE_B)};
  assert.equal(foreignBinding(state, message), true);
  assert.equal(applyEvent(state, message).accept, false);
  assert.equal(applyEvent(state, {type: 'audio', wav: 'AAAA', turn: 1, binding: compact(CASE_B)}).accept, false);
  const otherCompany = {type: 'audio', wav: 'AAAA', turn: 1, binding: {...compact(), company_id: 'ten_' + '2'.repeat(32)}};
  assert.equal(applyEvent(state, otherCompany).accept, false);  // the company changed while synthesis was running
  const mine = applyEvent(state, {type: 'audio', wav: 'AAAA', turn: 1, binding: compact()});
  assert.equal(mine.accept, true);
  assert.deepEqual(mine.effects, ['play']);
});

test('the plain assistant keeps working without any binding', () => {
  let state = initialConversation();
  const events = [{type: 'status', state: 'processing', turn: 1}, {type: 'message', role: 'assistant', text: 'oi', turn: 1, binding: {mode: 'local', company_id: null, case_id: null}},
    {type: 'status', state: 'ready', turn: 1}];
  const result = run(state, events);
  assert.deepEqual(result.log.map(l => l.accept), [true, true, true]);
  assert.equal(result.state.running, false);
  assert.equal(applyEvent(state, {type: 'message', role: 'user', text: 'legado'}).accept, true);  // events without a turn id still work
});

test('latency is measured from sending to the first assistant message, once per turn', () => {
  let state = startTurn(initialConversation(), 1000);
  state = applyEvent(state, {type: 'message', role: 'assistant', turn: 1, text: 'a'}, 1450).state;
  assert.equal(state.latencyMs, 450);
  state = applyEvent(state, {type: 'message', role: 'assistant', turn: 1, text: 'b'}, 3000).state;
  assert.equal(state.latencyMs, 450);  // the second message of the same answer does not overwrite it
  assert.deepEqual(state.latencies, [450]);
  assert.equal(median([300, 100, 200]), 200);
  assert.equal(median([100, 200]), 150);
  assert.equal(median([]), null);
});

test('an agent change or any event never turns the microphone on', () => {
  const sequence = [{type: 'binding', binding: binding(CASE_A, 'Suporte'), reason: 'vinculo'}, {type: 'binding', binding: binding(CASE_A, 'Financeiro'), reason: 'mudou'},
    {type: 'status', state: 'processing', turn: 1}, {type: 'audio', wav: 'AA', turn: 1, binding: compact()}, {type: 'status', state: 'ready', turn: 1},
    {type: 'error', message: 'x', fatal: true, turn: 1}, {type: 'cancelled'}, {type: 'rejected', message: 'x'}];
  const {log} = run(initialConversation(), sequence);
  for (const entry of log) assert.ok(!entry.effects.some(effect => /mic/i.test(effect)), JSON.stringify(entry.e));
});

test('stop speech, interrupt, pause case and microphone are four separate controls', () => {
  const idle = {...initialConversation(), binding: binding()};
  assert.deepEqual(controls(idle, {speaking: false, fatal: false, connection: 'ready'}), {stopSpeech: false, interrupt: false, pauseCase: true, microphone: true});
  const speaking = controls(idle, {speaking: true, fatal: false, connection: 'ready'});
  assert.equal(speaking.stopSpeech, true);
  assert.equal(speaking.interrupt, false);  // speaking is not generating
  const generating = controls({...idle, running: true}, {speaking: false, fatal: false, connection: 'processing'});
  assert.equal(generating.interrupt, true);
  assert.equal(generating.stopSpeech, false);
  assert.equal(controls({...idle, binding: binding(CASE_A, 'S', {paused: true})}, {connection: 'ready'}).pauseCase, false);
  assert.equal(controls({...idle, binding: binding(CASE_A, 'S', {case: {id: CASE_A, label: 'x', phase: 'concluida'}})}, {connection: 'ready'}).pauseCase, false);
  assert.equal(controls(initialConversation(), {connection: 'ready'}).pauseCase, false);  // nothing attached: nothing to pause
  assert.equal(controls(idle, {connection: 'offline'}).microphone, false);
});

test('reconnecting forgets the binding but remembers the choice, and asks to attach again', () => {
  let state = select(applyEvent(initialConversation(), {type: 'binding', binding: binding()}).state, {company_id: CO, case_id: CASE_A, extra: 'ignored'});
  state = startTurn(state, 5);
  const after = reconnected(state);
  assert.equal(after.binding, null);
  assert.equal(after.running, false);
  assert.deepEqual(reattachMessage(after), {type: 'attach', company_id: CO, case_id: CASE_A});
  assert.equal(reattachMessage(initialConversation()), null);
  assert.equal(select(after, null).selection, null);
});

test('labels and the avatar status describe the conversation and the case, without content', () => {
  assert.equal(bindingLabel(null), 'Assistente local');
  assert.equal(bindingLabel(binding()), 'Empresa A · Site fora do ar · Suporte');
  assert.equal(bindingLabel({...binding(), agent: null}), 'Empresa A · Site fora do ar · sem responsável');
  const status = avatarStatus({connection: 'ready', micEnabled: true, speaking: false, level: 0.4, binding: binding(CASE_A, 'Suporte', {waiting_human: true})});
  assert.deepEqual(status, {state: 'listening', micEnabled: true, level: 0.4, waitingHuman: true, caseRef: {company_id: CO, case_id: CASE_A},
    caseLabel: 'Site fora do ar', agentName: 'Suporte'});
  assert.equal(avatarStatus({connection: 'ready', speaking: true, micEnabled: true, binding: null}).state, 'speaking');
  assert.equal(avatarStatus({connection: 'processing', micEnabled: true, binding: null}).state, 'processing');
  assert.equal(avatarStatus({connection: 'ready', binding: null}).caseRef, null);
  assert.equal(isCaseRef({company_id: CO, case_id: CASE_A}), true);
  for (const bad of [null, {}, {company_id: CO, case_id: 'x'}, {company_id: 'ten_zz', case_id: CASE_A}, {company_id: CO + 'x', case_id: CASE_A}]) assert.equal(isCaseRef(bad), false);
});
