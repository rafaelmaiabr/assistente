import test from 'node:test';
import assert from 'node:assert/strict';
import {applyRulesEvent, decisionMessage, editMessage, initialRules, isRulesEvent, toggleMessage} from '../src/rulesModel.js';

const rule = {id: 'r-1', status: 'ativa', descricao: 'mensagens de o endereço ana@empresa.com'};

test('the server state replaces the screen state and a refusal shows its reason', () => {
  const saved = applyRulesEvent(initialRules, {type: 'rules', ok: true, rules: [rule], pending: []});
  assert.deepEqual(saved.rules, [rule]);
  const refused = applyRulesEvent(saved, {type: 'rules', ok: false, erro: 'Já existe uma regra igual.', rules: [rule], pending: []});
  assert.equal(refused.message, 'Já existe uma regra igual.');
  assert.deepEqual(refused.rules, [rule]);
});

test('each choice says what happened and the only-this-time choice says nothing was saved', () => {
  assert.match(applyRulesEvent(initialRules, {type: 'rules', ok: true, decidida: 'criada', rules: [rule], pending: []}).message, /Regra criada/);
  assert.match(applyRulesEvent(initialRules, {type: 'rules', ok: true, decidida: 'uma_vez', rules: [], pending: []}).message, /Nada foi salvo/);
  assert.match(applyRulesEvent(initialRules, {type: 'rules', ok: true, decidida: 'recusada', rules: [], pending: []}).message, /Nada foi salvo/);
});

test('a proposal from the chat is a pending card once and never a rule', () => {
  const proposal = {id: 'p-1', pergunta: 'Criar regra para ...?'};
  const once = applyRulesEvent(initialRules, {type: 'rule_proposal', proposal});
  assert.deepEqual(once.pending, [proposal]);
  assert.deepEqual(once.rules, []);
  assert.equal(applyRulesEvent(once, {type: 'rule_proposal', proposal}).pending.length, 1);
  assert.equal(isRulesEvent({type: 'rule_proposal'}), true);
  assert.equal(isRulesEvent({type: 'memory'}), false);
  assert.equal(applyRulesEvent(once, {type: 'message'}), once);
});

test('the messages are built only for valid input', () => {
  assert.deepEqual(decisionMessage('p-1', 'uma_vez'), {type: 'rule_decision', id: 'p-1', decision: 'uma_vez'});
  assert.equal(decisionMessage('p-1', 'talvez'), null);
  assert.equal(decisionMessage('', 'criar'), null);
  assert.deepEqual(toggleMessage('r-1', false), {type: 'rule_toggle', id: 'r-1', active: false});
  assert.equal(toggleMessage('r-1', 'sim'), null);
  assert.deepEqual(editMessage('r-1', {valor: ' fatura ', excecoes: 'rascunho; ; teste', acao: 'preservar'}),
    {type: 'rule_edit', id: 'r-1', excecoes: ['rascunho', 'teste'], valor: 'fatura', acao: 'preservar'});
  assert.deepEqual(editMessage('r-1', {valor: '', excecoes: '', acao: 'apagar tudo'}), {type: 'rule_edit', id: 'r-1', excecoes: []});
  assert.equal(editMessage('', {}), null);
});
