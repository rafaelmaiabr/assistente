import test from 'node:test';
import assert from 'node:assert/strict';
import {canAt, loginErrorFrom, loginMessage, sessionKind, visibleTabs} from '../src/team/authModel.js';

const TABS = [['cases', 'Demandas'], ['departments', 'Departamentos'], ['agents', 'Agentes'], ['skills', 'Skills'], ['connections', 'Conexões'], ['members', 'Membros']];

test('roles are ordered and the local owner is not limited by them', () => {
  assert.equal(canAt('leitor', 'leitor'), true);
  assert.equal(canAt('leitor', 'atendente'), false);
  assert.equal(canAt('aprovador', 'atendente'), true);
  assert.equal(canAt('aprovador', 'admin'), false);
  assert.equal(canAt('admin', 'aprovador'), true);
  assert.equal(canAt('owner', 'admin'), true);
  assert.equal(canAt(undefined, 'admin'), true);
});

test('each role sees only the sections it can use', () => {
  const ids = role => visibleTabs(TABS, role).map(([id]) => id);
  assert.deepEqual(ids('leitor'), ['cases']);
  assert.deepEqual(ids('atendente'), ['cases']);
  assert.deepEqual(ids('aprovador'), ['cases']);
  assert.deepEqual(ids('admin'), ['cases', 'departments', 'agents', 'skills', 'connections', 'members']);
  assert.deepEqual(ids('owner'), ids('admin'));
});

test('only known sign-in error codes ever reach the screen', () => {
  assert.equal(loginErrorFrom('?login_error=conta_nao_autorizada'), 'conta_nao_autorizada');
  assert.equal(loginErrorFrom('?login_error=<script>alert(1)</script>'), '');
  assert.equal(loginErrorFrom('?x=1'), '');
  assert.equal(loginErrorFrom(''), '');
  assert.equal(loginErrorFrom('?login_error=constructor'), '');
  assert.match(loginMessage('conta_nao_autorizada'), /cadastrar/);
  assert.equal(loginMessage('qualquer'), '');
});

test('the page learns from the server whether it is local, remote signed in, or needs the sign-in', () => {
  assert.equal(sessionKind(200, {mode: 'local', authenticated: true}), 'local');
  assert.equal(sessionKind(200, {mode: 'remote', authenticated: true}), 'remote');
  assert.equal(sessionKind(401, {mode: 'remote', authenticated: false}), 'login');
  assert.equal(sessionKind(404, null), 'unreachable');
  assert.equal(sessionKind(200, {app: 'antigo'}), 'local');
});
