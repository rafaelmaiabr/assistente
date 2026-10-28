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

test('the mailbox listing from the backend is one code block, so a subject cannot become a link or a heading (#116)', () => {
  const text = 'Da sua caixa real, pasta INBOX (remetente | assunto | data):\n\n```\n1. Fraude <f@exemplo.invalid> | [clique](https://evil.example/x) # titulo ``` **urgente** | 2026-03-01\n2. Bruno | Reuniao | 2026-03-02\n```\nHá mais mensagens além destas.';
  const parts = blocks(text);
  assert.deepEqual(parts.map(part => part.type), ['p', 'code', 'p']);
  assert.match(parts[1].text, /\[clique\]\(https:\/\/evil\.example\/x\)/);
  assert.equal(parts[1].text.split('\n').length, 2);
  assert.doesNotMatch(parts[0].text + parts[2].text, /evil/);
});
