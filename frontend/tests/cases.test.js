import test from 'node:test';
import assert from 'node:assert/strict';
import {actionLabel, approvalLines, canDecide, canDecideGap, canFulfillGap, canRevoke, gapDecisionBody, gapDecisionLabel, gapState, describeEvent, formatMoney, mergeEvents, needsRefresh, nextDelay,
  shortHash, touchesCase} from '../src/team/casesModel.js';

const ev = (seq, id = `evt_${seq}`, case_id = 'cas_1') => ({seq, id, case_id, type: 'case.state-changed'});

test('duplicate and out-of-order events end up unique, ordered, with a cursor that never goes back', () => {
  const first = mergeEvents([], [ev(3), ev(1), ev(2)]);
  assert.deepEqual(first.events.map(e => e.seq), [1, 2, 3]);
  assert.equal(first.cursor, 3);
  const again = mergeEvents(first.events, [ev(2), ev(3), ev(5), ev(4)], first.cursor);
  assert.deepEqual(again.events.map(e => e.seq), [1, 2, 3, 4, 5]);
  assert.equal(again.cursor, 5);
  const late = mergeEvents(again.events, [ev(1)], again.cursor);  // a stale replay after a reconnect
  assert.equal(late.cursor, 5);
  assert.equal(late.events.length, 5);
  assert.equal(mergeEvents([], [], 7).cursor, 7);
});

test('only new events ask for a re-read of the authoritative state', () => {
  assert.equal(needsRefresh([]), false);
  assert.equal(needsRefresh([ev(1)]), true);
  assert.equal(touchesCase([ev(1, 'a', 'cas_9')], 'cas_1'), false);
  assert.equal(touchesCase([ev(1), ev(2, 'b', 'cas_9')], 'cas_9'), true);
});

test('polling backs off while failing and recovers to the steady rate', () => {
  assert.equal(nextDelay(0), 3000);
  assert.deepEqual([1, 2, 3, 4, 5, 9].map(n => nextDelay(n)), [6000, 12000, 24000, 30000, 30000, 30000]);
  assert.equal(nextDelay(0, 1000), 1000);
});

test('an approval is shown as the concrete action, recipient, amount and parameters', () => {
  const approval = {action: 'enviar_mensagem', recipient: {to: ['a@x.com', 'b@x.com']}, amount_cents: 15000, currency: 'brl',
    resource: 'con_1', params: {assunto: 'Cobranca', n: 2}, expires_at: '2026-06-02T12:00:00Z', state: 'pendente', payload_hash: 'a'.repeat(64)};
  const lines = Object.fromEntries(approvalLines(approval));
  assert.equal(lines['Ação'], 'Enviar mensagem ao cliente');
  assert.equal(lines['Destinatário(s)'], 'a@x.com, b@x.com');
  assert.match(lines['Valor'], /^R\$\s150,00$/);
  assert.equal(lines.assunto, 'Cobranca');
  assert.equal(lines.n, '2');
  assert.equal(shortHash(approval.payload_hash), 'aaaaaaaa…aaaaaa');
  assert.equal(canDecide(approval), true);
  assert.equal(canDecide({...approval, state: 'aprovada'}), false);
  assert.equal(canRevoke({...approval, state: 'aprovada'}), true);
  assert.equal(canRevoke({...approval, state: 'consumida'}), false);
});

test('labels fall back to the raw name instead of hiding unknown things', () => {
  assert.equal(actionLabel('ferramenta:teste.cobrar:enviar-cobranca'), 'Usar ferramenta enviar-cobranca');
  assert.equal(actionLabel('algo_novo'), 'algo_novo');
  assert.equal(describeEvent('approval.requested'), 'Aprovação solicitada');
  assert.equal(describeEvent('x.y'), 'x.y');
  assert.equal(formatMoney(null, 'brl'), '');
  assert.match(formatMoney(100, 'usd'), /^US\$/);
});

test('a gap is decided once, always with a reason, and only skill or tool gaps can be fulfilled', () => {
  const open = {state: 'aberta', revision: 3, decision: null};
  assert.equal(canDecideGap(open), true);
  assert.equal(canFulfillGap(open), false);
  assert.deepEqual(gapDecisionBody(open, 'criar_skill', '  vale a pena ', '  issue 9 '), {expected_revision: 3, decision: 'criar_skill', note: 'vale a pena', follow_up: 'issue 9'});
  assert.deepEqual(gapDecisionBody(open, 'nao_atender', 'fora do servico', '  '), {expected_revision: 3, decision: 'nao_atender', note: 'fora do servico'});
  const decided = {state: 'decidida', revision: 4, decision: 'criar_ferramenta'};
  assert.equal(canDecideGap(decided), false);
  assert.equal(canFulfillGap(decided), true);
  assert.equal(canFulfillGap({state: 'decidida', decision: 'nao_atender'}), false);
  assert.equal(gapState(open), 'Aguardando decisão');
  assert.equal(gapState(decided), 'Decidida: Criar uma ferramenta');
  assert.equal(gapState({state: 'atendida'}), 'Atendida');
  assert.equal(gapDecisionLabel('nada'), '');
});
