import React, {useState} from 'react';
import {api} from './api.js';
import {describeError, operationsLabel, permissionLines} from './model.js';
import {Notice, useList} from './shared.jsx';

const RISKS = {dados_pessoais: 'dados pessoais', dados_financeiros: 'dados financeiros', leitura_email: 'leitura de email',
  envio_externo: 'envio externo', pagamento: 'pagamento', alteracao_dominio: 'alteração de domínio'};

function Review({review, onInstall, onCancel, busy}) {
  const [confirmed, setConfirmed] = useState(false), [confirmedNew, setConfirmedNew] = useState(false);
  const added = review.added_permissions.length > 0 && review.previous_version;
  return <div className="team-review" role="region" aria-label={`Prévia de ${review.skill_key}`}>
    <h4>{review.skill_key} {review.skill_version} {review.fictitious && <span className="badge demo">demonstração · dados fictícios</span>}</h4>
    <p>{review.description}</p>
    <h5>Permissões que esta skill pede</h5>
    <ul>{permissionLines(review).map(line => <li key={line}>{line}</li>)}</ul>
    <p>Riscos: {review.risks.length ? review.risks.map(r => RISKS[r] || r).join(', ') : 'nenhum declarado'}. Instalar não concede acesso: cada agente precisa de uma concessão explícita.</p>
    {added && <p className="team-error">A nova versão pede permissões adicionais em relação à {review.previous_version}: {review.added_permissions.map(p => `${p.tool}:${p.operation}`).join(', ')}.</p>}
    <label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}/> Revisei as permissões e os riscos.</label>
    {added && <label className="check"><input type="checkbox" checked={confirmedNew} onChange={e => setConfirmedNew(e.target.checked)}/> Aceito as permissões adicionais.</label>}
    <div className="team-actions">
      <button className="primary" disabled={busy || !confirmed || (added && !confirmedNew)} onClick={() => onInstall(review, confirmedNew)}>Instalar esta versão</button>
      <button onClick={onCancel}>Cancelar</button></div>
  </div>;
}

function Grants({base, agents, connections, skills}) {
  const [agentId, setAgentId] = useState(''), [form, setForm] = useState({skill_id: '', connection_id: '', operations: ['read'], resource: '', attach: true, confirmed: false});
  const grants = useList(agentId ? `${base}/grants?agent_id=${agentId}` : '', [agentId]);
  const [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const skill = skills.find(s => s.id === form.skill_id);
  const needsConnection = skill && Object.keys(skill.tool_operations || {}).length > 0;
  const run = async (action, ok) => {setBusy(true); setMessage({}); try {await action(); setMessage({ok}); await grants.reload();} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}};
  const grant = () => run(async () => {
    const body = {agent_id: agentId, skill_id: form.skill_id, operations: form.operations, attach: form.attach};
    if (form.connection_id) body.connection_id = form.connection_id;
    if (form.resource.trim()) body.resource = form.resource.trim();
    await api.post(`${base}/grants`, body); setForm({...form, confirmed: false});
  }, 'Concessão criada para a versão fixa da skill.');
  const names = Object.fromEntries(skills.map(s => [s.id, `${s.skill_key} ${s.skill_version}`]));
  return <section aria-labelledby="grants-title"><h3 id="grants-title">Concessões por agente</h3>
    <label>Agente<select value={agentId} onChange={e => setAgentId(e.target.value)}><option value="">Selecione um agente</option>
      {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
    {agentId && <>
      <ul className="team-list compact">{grants.data.map(g => <li key={g.id}><span>{names[g.skill_id] || g.skill_id} — {operationsLabel(g.operations)}
        {g.revoked_at ? <span className="badge off">revogada</span> : <span className="badge">ativa</span>}</span>
        {!g.revoked_at && <button disabled={busy} onClick={() => run(() => api.del(`${base}/grants/${g.id}`), 'Concessão revogada: novas chamadas ficam bloqueadas.')}>Revogar</button>}</li>)}
        {!grants.data.length && <li className="team-empty">Este agente não tem skills concedidas.</li>}</ul>
      <form className="team-form" onSubmit={e => {e.preventDefault(); void grant();}}>
        <h4>Conceder uma skill</h4>
        <label>Skill<select value={form.skill_id} onChange={e => setForm({...form, skill_id: e.target.value})}><option value="">Selecione</option>
          {skills.filter(s => s.status === 'ativo').map(s => <option key={s.id} value={s.id}>{s.skill_key} {s.skill_version}</option>)}</select></label>
        <label>Conta (conexão){needsConnection ? '' : ' — opcional'}<select value={form.connection_id} onChange={e => setForm({...form, connection_id: e.target.value})}>
          <option value="">Nenhuma</option>{connections.map(c => <option key={c.id} value={c.id}>{c.label} ({c.status})</option>)}</select></label>
        <fieldset><legend>Operações permitidas</legend>
          {(skill?.operations || ['read']).map(op => <label key={op} className="check"><input type="checkbox" checked={form.operations.includes(op)}
            onChange={e => setForm({...form, operations: e.target.checked ? [...form.operations, op] : form.operations.filter(o => o !== op)})}/> {operationsLabel([op])}</label>)}</fieldset>
        <label>Recurso (opcional)<input value={form.resource} onChange={e => setForm({...form, resource: e.target.value})} placeholder="ex.: cobrancas"/></label>
        <label className="check"><input type="checkbox" checked={form.attach} onChange={e => setForm({...form, attach: e.target.checked})}/> Publicar nova versão do agente com esta concessão</label>
        <label className="check"><input type="checkbox" checked={form.confirmed} onChange={e => setForm({...form, confirmed: e.target.checked})}/> Entendo que o agente passará a poder usar esta skill com a conta e as operações escolhidas.</label>
        <Notice error={message.error} ok={message.ok}/>
        <button type="submit" className="primary" disabled={busy || !form.skill_id || !form.operations.length || !form.confirmed || (needsConnection && !form.connection_id)}>Conceder</button>
      </form></>}
  </section>;
}

export function SkillsSection({base}) {
  const skills = useList(`${base}/skills`), demos = useList(`${base}/skills/demos`);
  const agents = useList(`${base}/agents`), connections = useList(`${base}/connections`);
  const [review, setReview] = useState(null), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, ok) => {setBusy(true); setMessage({}); try {await action(); if (ok) setMessage({ok});} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}};
  const preview = demo => run(async () => setReview({...(await api.post(`${base}/skills/review`, {demo})), demo}));
  const install = (r, acceptNew) => run(async () => {
    await api.post(`${base}/skills/install`, {demo: r.demo, approved_hash: r.content_hash, accept_new_permissions: acceptNew});
    setReview(null); await skills.reload();
  }, 'Skill instalada. Ela ainda não foi concedida a nenhum agente.');
  return <section aria-labelledby="skills-title">
    <h2 id="skills-title">Skills</h2>
    <Notice error={skills.error || message.error} ok={message.ok}/>
    <h3>Instaladas</h3>
    <ul className="team-list">{skills.data.map(s => <li key={s.id}><div><strong>{s.skill_key}</strong> {s.skill_version}
      {s.fictitious && <span className="badge demo">demonstração</span>} <span className={`badge ${s.status === 'ativo' ? '' : 'off'}`}>{s.status}</span><p>{s.description}</p></div>
      <button disabled={busy} onClick={() => run(async () => {await api.patch(`${base}/skills/${s.id}`, {status: s.status === 'ativo' ? 'inativo' : 'ativo'}); await skills.reload();},
        s.status === 'ativo' ? 'Skill desativada: novas chamadas ficam bloqueadas.' : 'Skill reativada.')}>{s.status === 'ativo' ? 'Desativar' : 'Reativar'}</button></li>)}
      {!skills.data.length && <li className="team-empty">Nenhuma skill instalada.</li>}</ul>
    <h3>Pacotes de demonstração</h3>
    <p className="hint">Pacotes fictícios para experimentar. Revise as permissões antes de instalar.</p>
    <ul className="team-list">{demos.data.map(d => <li key={d.demo}><div><strong>{d.skill_key}</strong> {d.skill_version} <span className="badge demo">demonstração · dados fictícios</span><p>{d.description}</p></div>
      <button disabled={busy} onClick={() => preview(d.demo)}>Revisar permissões</button></li>)}</ul>
    {review && <Review review={review} busy={busy} onInstall={install} onCancel={() => setReview(null)}/>}
    <Grants base={base} agents={agents.data} connections={connections.data} skills={skills.data}/>
  </section>;
}
