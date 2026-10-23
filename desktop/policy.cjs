const BASE_URL = 'http://127.0.0.1:8765';
const STATES = new Set(['connecting','offline','error','ready','processing','executing','transcribing','synthesizing','speaking','listening']);
function trustedChat(url) {
  try {const parsed = new URL(url); return parsed.origin === BASE_URL && parsed.pathname === '/';} catch {return false;}
}
const ID = /^[a-z]{3}_[0-9a-f]{32}$/;
// Only ids with the exact shape the backend generates and short plain labels cross to the avatar window.
const label = (text, max) => typeof text === 'string' ? text.replace(/[\u0000-\u001f\u007f<>]/g, ' ').trim().slice(0, max) || null : null;
function sanitizeStatus(value) {
  if (!value || !STATES.has(value.state)) return null;
  const ref = value.caseRef;
  const caseRef = ref && ID.test(ref.company_id) && ID.test(ref.case_id) ? {company_id: ref.company_id, case_id: ref.case_id} : null;
  return {state: value.state, micEnabled: value.micEnabled === true,
    level: Number.isFinite(value.level) ? Math.max(0, Math.min(1,value.level)) : 0,
    waitingHuman: value.waitingHuman === true, caseRef, caseLabel: caseRef ? label(value.caseLabel, 80) : null,
    agentName: caseRef ? label(value.agentName, 60) : null};
}
function audioPermission(url, types) {
  return trustedChat(url) && Array.isArray(types) && types.length > 0 && types.every(type => type === 'audio');
}
module.exports = {BASE_URL, trustedChat, sanitizeStatus, audioPermission};
