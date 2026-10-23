import React from 'react';
import {blocks} from './markdownModel.js';

// Safe Markdown for the chat: it builds React elements (never HTML strings), supports the marks a model actually uses
// (headings, lists, bold, italic, inline code, code blocks, links) and shows anything else as plain text.

const SAFE_LINK = /^https?:\/\//i;

/** Inline marks inside one line of text. */
export function inline(text, keyBase = 'i') {
  const out = [];
  const pattern = /(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0, match, n = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0], key = `${keyBase}${n++}`;
    if (token.startsWith('**') || token.startsWith('__')) out.push(<strong key={key}>{inline(token.slice(2, -2), key)}</strong>);
    else if (token.startsWith('`')) out.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (token.startsWith('[')) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      out.push(SAFE_LINK.test(link[2]) ? <a key={key} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a> : link[1]);
    } else out.push(<em key={key}>{inline(token.slice(1, -1), key)}</em>);
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({text}) {
  return <>{blocks(text).map((block, index) => {
    const key = `b${index}`;
    if (block.type === 'code') return <pre key={key}><code>{block.text}</code></pre>;
    if (block.type === 'heading') return React.createElement(`h${block.level}`, {key}, inline(block.text, key));
    if (block.type === 'ul' || block.type === 'ol') return React.createElement(block.type, {key}, block.items.map((item, n) => <li key={`${key}-${n}`}>{inline(item, `${key}-${n}-`)}</li>));
    return <p key={key}>{block.text.split('\n').flatMap((line, n) => (n ? [<br key={`${key}-br${n}`}/>, ...inline(line, `${key}-${n}-`)] : inline(line, `${key}-${n}-`)))}</p>;
  })}</>;
}
