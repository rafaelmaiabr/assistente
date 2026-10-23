import React, {useEffect, useMemo, useState} from 'react';
import {api} from './api.js';
import {agentDraft, agentPayload, blankAgent, changedFields, describeError, validateAgent} from './model.js';
import {Notice, useList} from './shared.jsx';

const FIELD_NAMES = {department_id: 'departamento', name: 'nome', goals: 'objetivos', tone: 'tom', initiative: 'iniciativa', scope: 'escopo',
  out_of_scope: 'fora do escopo', routing_policy: 'encaminhamento', model_policy_ref: 'modelo', voice_profile_ref: 'voz',
  handoff_target_ids: 'destinos de encaminhamento', capabilities: 'capacidades', status: 'status'};

export function AgentsSection({base}) {
  const agents = useList(`${base}/agents`), departments = useList(`${base}/departments`);
  const [options, setOptions] = useState({voices: [], model_policies: [], routing_policies: []});
  const [selected, setSelected] = useState(null), [draft, setDraft] = useState(null);
  const [versions, setVersions] = useState([]), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  useEffect(() => {api.get(`${base}/agent-options`).then(setOptions).catch(e => setMessage({error: describeError(e)}));}, [base]);
  const original = useMemo(() => agents.data.find(a => a.id === selected), [agents.data, selected]);
  const edit = patch => {setDraft(d => ({...d, ...patch})); setMessage({});};
  const open = agent => {
    setSelected(agent.id); setDraft(agentDraft(agent)); setMessage({});
    api.get(`${base}/agents/${agent.id}/versions`).then(setVersions).catch(() => setVersions([]));
  };
  const startNew = () => {setSelected('novo'); setDraft(blankAgent(departments.data.find(d => d.status === 'ativo')?.id)); setVersions([]); setMessage({});};
  const problems = draft ? validateAgent(draft) : [];
  const run = async (action, ok) => {
    setBusy(true); setMessage({});
    try {await action(); setMessage({ok});} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}
  };
  const validate = () => run(async () => {await api.post(`${base}/agents/validate`, {...agentPayload(draft), ...(original ? {agent_id: original.id} : {})});}, 'Tudo certo: as referências e o perfil são válidos. Nada foi salvo.');
  const save = () => run(async () => {
    const saved = original ? await api.patch(`${base}/agents/${original.id}`, {...agentPayload(draft), expected_revision: original.revision})
      : await api.post(`${base}/agents`, agentPayload(draft));
    await agents.reload(); setSelected(saved.id); setDraft(agentDraft(saved));
    setVersions(await api.get(`${base}/agents/${saved.id}/versions`));
  }, original ? 'Nova versão publicada. Execuções anteriores continuam na versão que usaram.' : 'Agente criado (versão 1).');
  const toggleVersion = version => run(async () => {
    await api.patch(`${base}/agents/${original.id}/versions/${version.agent_version}`, {status: version.status === 'ativo' ? 'inativo' : 'ativo'});
    setVersions(await api.get(`${base}/agents/${original.id}/versions`));
  }, version.status === 'ativo' ? 'Versão desativada para novas execuções.' : 'Versão reativada.');
  const changes = original && draft ? changedFields(original, draft) : [];
  const otherAgents = agents.data.filter(a => a.id !== selected);

  return <section aria-labelledby="agents-title" className="team-split">
    <div>
      <h2 id="agents-title">Agentes</h2>
      <Notice error={agents.error || departments.error}/>
      <ul className="team-list">
        {agents.data.map(a => <li key={a.id} className={a.id === selected ? 'selected' : ''}>
          <button className="link" onClick={() => open(a)}><strong>{a.name}</strong> <span className="badge">v{a.agent_version}</span>
            {a.status !== 'ativo' && <span className="badge off">{a.status}</span>}</button></li>)}
        {!agents.data.length && <li className="team-empty">Nenhum agente ainda.</li>}
      </ul>
      <button onClick={startNew} disabled={!departments.data.some(d => d.status === 'ativo')}>Novo agente</button>
      {!departments.data.some(d => d.status === 'ativo') && <p className="hint">Crie um departamento antes.</p>}
    </div>
    {draft && <form className="team-form" onSubmit={e => {e.preventDefault(); void save();}} aria-label="Perfil do agente">
      <h3>{original ? `Editar ${original.name}` : 'Novo agente'}</h3>
      <label>Nome<input maxLength={120} value={draft.name} onChange={e => edit({name: e.target.value})}/></label>
      <label>Departamento<select value={draft.department_id} onChange={e => edit({department_id: e.target.value})}>
        {departments.data.map(d => <option key={d.id} value={d.id}>{d.name}{d.status === 'ativo' ? '' : ' (inativo)'}</option>)}</select></label>
      <label>Objetivos (um por linha)<textarea rows={3} value={draft.goals} onChange={e => edit({goals: e.target.value})}/></label>
      <div className="team-row"><label>Tom<input value={draft.tone} onChange={e => edit({tone: e.target.value})}/></label>
        <label>Iniciativa<input value={draft.initiative} onChange={e => edit({initiative: e.target.value})}/></label></div>
      <label>Escopo<textarea rows={2} value={draft.scope} onChange={e => edit({scope: e.target.value})}/></label>
      <label>Fora do escopo<textarea rows={2} value={draft.out_of_scope} onChange={e => edit({out_of_scope: e.target.value})}/></label>
      <div className="team-row">
        <label>Se o pedido for de outro assunto<select value={draft.routing_policy} onChange={e => edit({routing_policy: e.target.value})}>
          <option value="encaminhar">Oferecer encaminhamento</option><option value="esclarecer">Pedir esclarecimento</option></select></label>
        <label>Política de modelo<select value={draft.model_policy_ref} onChange={e => edit({model_policy_ref: e.target.value})}>
          {options.model_policies.map(p => <option key={p} value={p}>{p}</option>)}</select></label>
        <label>Voz<select value={draft.voice_profile_ref} onChange={e => edit({voice_profile_ref: e.target.value})}>
          <option value="">Sem voz definida</option>{options.voices.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}</select></label>
      </div>
      <fieldset><legend>Pode encaminhar para</legend>
        {otherAgents.map(a => <label key={a.id} className="check"><input type="checkbox" checked={draft.handoff_target_ids.includes(a.id)}
          onChange={e => edit({handoff_target_ids: e.target.checked ? [...draft.handoff_target_ids, a.id] : draft.handoff_target_ids.filter(i => i !== a.id)})}/> {a.name}</label>)}
        {!otherAgents.length && <p className="hint">Nenhum outro agente.</p>}</fieldset>
      <label>Capacidades (separadas por vírgula)<input value={draft.capabilities} onChange={e => edit({capabilities: e.target.value})} placeholder="ex.: financeiro, suporte"/></label>
      {original && <label>Status<select value={draft.status} onChange={e => edit({status: e.target.value})}><option value="ativo">Ativo</option><option value="inativo">Inativo</option></select></label>}
      {changes.length > 0 && <p className="hint" aria-live="polite">Alterações não salvas: {changes.map(c => FIELD_NAMES[c] || c).join(', ')}.</p>}
      {problems.length > 0 && <ul className="team-error" aria-label="Pendências">{problems.map(p => <li key={p}>{p}</li>)}</ul>}
      <Notice error={message.error} ok={message.ok}/>
      <div className="team-actions">
        <button type="button" onClick={validate} disabled={busy || problems.length > 0}>Validar</button>
        <button type="submit" className="primary" disabled={busy || problems.length > 0 || (original && !changes.length)}>{original ? 'Publicar nova versão' : 'Criar agente'}</button>
        <button type="button" onClick={() => {setDraft(null); setSelected(null);}}>Fechar</button>
      </div>
      {original && <section aria-label="Versões publicadas"><h4>Versões publicadas</h4>
        <ul className="team-list compact">{versions.map(v => <li key={v.id}><span>v{v.agent_version} <span className={`badge ${v.status === 'ativo' ? '' : 'off'}`}>{v.status}</span>
          <small> {v.created_at}</small></span>
          <button type="button" disabled={busy} onClick={() => toggleVersion(v)}>{v.status === 'ativo' ? 'Desativar' : 'Reativar'}</button></li>)}</ul>
        <p className="hint">Desativar uma versão só impede novas execuções com ela; o histórico não muda.</p></section>}
    </form>}
  </section>;
}
