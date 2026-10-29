// Answer style of the conversation (#136). It only shortens answers: the honesty notices and permissions are the server's and never change.
export const STYLES = [{id: 'padrao', label: 'Padrão'}, {id: 'curto', label: 'Respostas curtas'}];
export const DEFAULT_STYLE = 'padrao';

const known = id => STYLES.some(style => style.id === id);

// The style the server says is active (in the `session` and `style` events); anything unknown keeps the current one.
export function applyStyleEvent(current, event) {
  if ((event.type === 'session' || event.type === 'style') && known(event.style)) return event.style;
  return current;
}

// The message the select sends, or null when the choice is not a known style.
export function styleMessage(id) {
  return known(id) ? {type: 'style', style: id} : null;
}
