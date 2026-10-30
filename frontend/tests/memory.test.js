import test from 'node:test';
import assert from 'node:assert/strict';
import {addMessage, applyMemoryEvent, decisionMessage, deleteMessage, describeProposal, editMessage, initialMemory, isMemoryEvent} from '../src/memoryModel.js';

const entry = {id: 'a1', text: 'Prefere respostas curtas'};

test('the server state replaces the screen state and a refusal shows its reason', () => {
  const saved = applyMemoryEvent(initialMemory, {type: 'memory', ok: true, entries: [entry], pending: [], used: 24, limit: 1400});
  assert.deepEqual(saved.entries, [entry]);
  assert.equal(saved.message, '');
  const refused = applyMemoryEvent(saved, {type: 'memory', ok: false, erro: 'O texto parece conter senha.', entries: [entry], pending: [], used: 24, limit: 1400});
  assert.equal(refused.message, 'O texto parece conter senha.');
  assert.deepEqual(refused.entries, [entry]);
});

test('approving says it only counts from the next conversation', () => {
  const state = applyMemoryEvent(initialMemory, {type: 'memory', ok: true, decidida: 'aprovada', entries: [entry], pending: []});
  assert.match(state.message, /próxima conversa/);
});

test('a proposal from the chat becomes a pending card once and never an entry', () => {
  const proposal = {id: 'p1', action: 'adicionar', text: 'Mora em Recife'};
  const once = applyMemoryEvent(initialMemory, {type: 'memory_proposal', proposal});
  assert.deepEqual(once.pending, [proposal]);
  assert.deepEqual(once.entries, []);
  assert.equal(applyMemoryEvent(once, {type: 'memory_proposal', proposal}).pending.length, 1);
  assert.equal(isMemoryEvent({type: 'memory_proposal'}), true);
  assert.equal(isMemoryEvent({type: 'message'}), false);
  assert.equal(applyMemoryEvent(once, {type: 'message'}), once);
});

test('the messages are built only for valid input', () => {
  assert.deepEqual(addMessage('  Gosta de café  '), {type: 'memory_add', text: 'Gosta de café'});
  assert.equal(addMessage('   '), null);
  assert.equal(addMessage('a'.repeat(201)), null);
  assert.deepEqual(editMessage('a1', 'Novo'), {type: 'memory_edit', id: 'a1', text: 'Novo'});
  assert.equal(editMessage('', 'Novo'), null);
  assert.deepEqual(deleteMessage('a1'), {type: 'memory_delete', id: 'a1'});
  assert.equal(deleteMessage(''), null);
  assert.deepEqual(decisionMessage('p1', 'approve'), {type: 'memory_decision', id: 'p1', decision: 'approve'});
  assert.equal(decisionMessage('p1', 'talvez'), null);
});

test('a proposal is described in words before the click', () => {
  assert.equal(describeProposal({action: 'adicionar', text: 'Mora em Recife'}), 'Lembrar: “Mora em Recife”');
  assert.equal(describeProposal({action: 'ajustar', text: 'Olinda', current: 'Recife'}), 'Trocar “Recife” por “Olinda”');
  assert.equal(describeProposal({action: 'remover', current: 'Recife'}), 'Esquecer: “Recife”');
});
