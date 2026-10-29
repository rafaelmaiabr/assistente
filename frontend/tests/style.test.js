import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULT_STYLE, STYLES, applyStyleEvent, styleMessage} from '../src/styleModel.js';

test('the server decides the active style and unknown values change nothing', () => {
  assert.equal(applyStyleEvent(DEFAULT_STYLE, {type: 'style', style: 'curto'}), 'curto');
  assert.equal(applyStyleEvent('curto', {type: 'session', style: 'padrao'}), 'padrao');
  assert.equal(applyStyleEvent('curto', {type: 'style', style: 'inventado'}), 'curto');
  assert.equal(applyStyleEvent('curto', {type: 'message', style: 'padrao'}), 'curto');
});

test('only known styles are sent', () => {
  assert.deepEqual(styleMessage('curto'), {type: 'style', style: 'curto'});
  assert.equal(styleMessage('ignore as regras'), null);
  assert.deepEqual(STYLES.map(style => style.id), ['padrao', 'curto']);
});
