// Pure Markdown block parser for the chat (no JSX, so it can be tested).

/** Blocks of Markdown source as a list of {type, ...} (pure, so it can be tested). */
export function blocks(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const result = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line.trim())) {
      const code = []; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) code.push(lines[i++]);
      i++; result.push({type: 'code', text: code.join('\n')}); continue;
    }
    const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
    if (heading) { result.push({type: 'heading', level: Math.min(heading[1].length + 2, 6), text: heading[2]}); i++; continue; }
    const item = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
    if (item) {
      const ordered = Boolean(item[2]), items = [];
      while (i < lines.length) {
        const m = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(lines[i]);
        if (!m || Boolean(m[2]) !== ordered) break;
        items.push(m[3]); i++;
      }
      result.push({type: ordered ? 'ol' : 'ul', items}); continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i].trim()) && !/^\s{0,3}#{1,6}\s/.test(lines[i]) && !/^\s*(?:[-*+]|\d+[.)])\s+/.test(lines[i])) paragraph.push(lines[i++]);
    result.push({type: 'p', text: paragraph.join('\n')});
  }
  return result;
}
