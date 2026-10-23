import test from 'node:test';
import assert from 'node:assert/strict';
import {blocks} from '../src/markdownModel.js';

test('lists, headings, code and paragraphs become blocks (the marks are not shown as text)', () => {
  const source = '## Organização\n1. **Categorização**: separamos os itens.\n2. **Caixas e Gavetas**: usem caixas.\n\nTexto com `código`.\n\n```\nlinha 1\n```\n- um\n- dois';
  assert.deepEqual(blocks(source), [
    {type: 'heading', level: 4, text: 'Organização'},
    {type: 'ol', items: ['**Categorização**: separamos os itens.', '**Caixas e Gavetas**: usem caixas.']},
    {type: 'p', text: 'Texto com `código`.'},
    {type: 'code', text: 'linha 1'},
    {type: 'ul', items: ['um', 'dois']},
  ]);
});

test('empty or odd input never throws', () => {
  assert.deepEqual(blocks(''), []);
  assert.deepEqual(blocks(null), []);
  assert.equal(blocks('```\nsem fechar').length, 1);
  assert.equal(blocks('texto\r\nmais texto')[0].type, 'p');
});
