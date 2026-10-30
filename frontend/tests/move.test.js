import test from 'node:test';
import assert from 'node:assert/strict';
import {applyMoveEvent, decisionMessage, initialMoves, isMoveEvent} from '../src/moveModel.js';

const proposal = {id: 'm-1', hash: 'a'.repeat(64), destino_pasta: 'Trash', itens: [{uid: 1, assunto: 'Oi', de: 'x'}]};

test('a proposal is a pending card once, and a reload brings the waiting ones back', () => {
  const once = applyMoveEvent(initialMoves, {type: 'move_proposal', proposal});
  assert.deepEqual(once.proposals, [proposal]);
  assert.equal(applyMoveEvent(once, {type: 'move_proposal', proposal}).proposals.length, 1);
  assert.deepEqual(applyMoveEvent(initialMoves, {type: 'move_proposals', proposals: [proposal]}).proposals, [proposal]);
  assert.equal(applyMoveEvent(once, {type: 'message'}), once);
  assert.equal(isMoveEvent({type: 'move_result'}), true);
  assert.equal(isMoveEvent({type: 'memory'}), false);
});

test('while it moves the card shows progress and then it is replaced by the summary', () => {
  const once = applyMoveEvent(initialMoves, {type: 'move_proposal', proposal});
  const moving = applyMoveEvent(once, {type: 'move_result', id: 'm-1', ok: true, estado: 'movendo'});
  assert.match(moving.status['m-1'], /Movendo/);
  assert.equal(moving.proposals.length, 1);
  const done = applyMoveEvent(moving, {type: 'move_result', id: 'm-1', ok: true, estado: 'concluida', movidas: 1, destino: 'Trash'});
  assert.deepEqual(done.proposals, []);
  assert.equal(done.status['m-1'], 'Movidas 1 mensagem(ns) para Trash.');
});

test('a stopped batch, a refused approval and a cancel each say what happened', () => {
  const once = applyMoveEvent(initialMoves, {type: 'move_proposal', proposal});
  const stopped = applyMoveEvent(once, {type: 'move_result', id: 'm-1', ok: false, estado: 'parada', movidas: 1, erro: 'A mensagem naquele número já não é a que foi aprovada.'});
  assert.match(stopped.status['m-1'], /Lote parado: A mensagem naquele número/);
  assert.match(stopped.status['m-1'], /Movidas até aqui: 1/);
  const altered = applyMoveEvent(once, {type: 'move_result', id: 'm-1', ok: false, estado: 'alterada', erro: 'A aprovação não corresponde. Nada foi movido.'});
  assert.equal(altered.status['m-1'], 'A aprovação não corresponde. Nada foi movido.');
  assert.equal(applyMoveEvent(once, {type: 'move_result', id: 'm-1', ok: true, estado: 'descartada'}).status['m-1'], 'Cancelado. Nada foi movido.');
});

test('approving sends the hash that was shown and cancelling sends none; bad input builds nothing', () => {
  assert.deepEqual(decisionMessage(proposal, 'approve'), {type: 'move_decision', id: 'm-1', decision: 'approve', hash: proposal.hash});
  assert.deepEqual(decisionMessage(proposal, 'discard'), {type: 'move_decision', id: 'm-1', decision: 'discard'});
  assert.equal(decisionMessage(proposal, 'talvez'), null);
  assert.equal(decisionMessage(null, 'approve'), null);
  assert.equal(decisionMessage({}, 'approve'), null);
});
