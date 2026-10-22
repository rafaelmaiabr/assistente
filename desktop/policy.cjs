const BASE_URL = 'http://127.0.0.1:8765';
const STATES = new Set(['connecting','offline','error','ready','processing','executing','transcribing','synthesizing','speaking','listening']);
function trustedChat(url) {
  try {const parsed = new URL(url); return parsed.origin === BASE_URL && parsed.pathname === '/';} catch {return false;}
}
function sanitizeStatus(value) {
  if (!value || !STATES.has(value.state)) return null;
  return {state: value.state, micEnabled: value.micEnabled === true,
    level: Number.isFinite(value.level) ? Math.max(0, Math.min(1,value.level)) : 0};
}
function audioPermission(url, types) {
  return trustedChat(url) && Array.isArray(types) && types.length > 0 && types.every(type => type === 'audio');
}
module.exports = {BASE_URL, trustedChat, sanitizeStatus, audioPermission};
