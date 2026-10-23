import React, {useEffect, useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';
import {STATE_CLASS, STATE_LABEL, canCreate, canPreview, doctorHeadline, planSummary, sortedChecks} from './onboardingModel.js';
import {Notice} from './shared.jsx';

/** Create a company, its departments and agents from a template, after reviewing exactly what will be created. */
export function OnboardingSection({onCreated}) {
  const [templates, setTemplates] = useState([]), [templateId, setTemplateId] = useState(''), [name, setName] = useState('');
  const [plan, setPlan] = useState(null), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  useEffect(() => {
    api.get('/onboarding/templates').then(data => {setTemplates(data.templates); setTemplateId(current => current || data.templates[0]?.id || '');})
      .catch(error => setMessage({error: describeError(error)}));
  }, []);
  const chosen = templates.find(t => t.id === templateId);
  const preview = async () => {
    setBusy(true); setMessage({}); setPlan(null);
    try {setPlan(await api.post('/onboarding/preview', {template_id: templateId, company_name: name.trim()}));}
    catch (error) {setMessage({error: describeError(error)});} finally {setBusy(false);}
  };
  const create = async () => {
    setBusy(true); setMessage({});
    try {
      const done = await api.post('/onboarding/apply', {template_id: templateId, company_name: name.trim(), plan_hash: plan.plan_hash});
      setPlan(null); setName(''); setMessage({ok: `Equipe criada: ${done.agents} agente(s) em ${done.departments} departamento(s).`});
      await onCreated(done.company.id);
    } catch (error) {setMessage({error: describeError(error)});} finally {setBusy(false);}
  };
  return <section className="team-form" aria-labelledby="onboarding-title">
    <h2 id="onboarding-title">Configurar a equipe por um modelo</h2>
    <p className="hint">Escolha um modelo e um nome. Antes de criar, você vê exatamente o que será criado. Nada é conectado a contas reais e nada é enviado a clientes.</p>
    <label>Modelo<select value={templateId} onChange={e => {setTemplateId(e.target.value); setPlan(null);}}>
      {templates.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}</select></label>
    {chosen && <p className="hint">{chosen.description}</p>}
    <label>Nome da empresa<input maxLength={120} value={name} onChange={e => {setName(e.target.value); setPlan(null);}} placeholder="Nome da empresa"/></label>
    <div className="team-actions"><button type="button" disabled={busy || !canPreview(templateId, name)} onClick={preview}>Pré-visualizar</button></div>
    <Notice error={message.error} ok={message.ok}/>
    {plan && <article className="team-approval" aria-label="Pré-visualização">
      <h3>O que será criado</h3>
      <ul className="team-list compact">{planSummary(plan).map(line => <li key={line}><span>{line}</span></li>)}</ul>
      <h4>Departamentos</h4><ul className="team-list compact">{plan.departments.map(d => <li key={d.key}><span><strong>{d.name}</strong>: {d.scope}</span></li>)}</ul>
      <h4>Agentes</h4><ul className="team-list compact">{plan.agents.map(a => <li key={a.key}><span><strong>{a.name}</strong> ({a.department})
        {a.capabilities.length > 0 && <> · capacidades: {a.capabilities.join(', ')}</>} · modelo local <code>{a.model_policy_ref}</code></span></li>)}</ul>
      {plan.handoffs.length > 0 && <><h4>Podem encaminhar entre si</h4><ul className="team-list compact">{plan.handoffs.map(h => <li key={h.de + h.para}><span>{h.de} → {h.para}</span></li>)}</ul></>}
      <h4>Acesso concedido</h4><ul className="team-list compact">{plan.case_access.map(g => <li key={g.key}><span>{g.agent}: {g.acesso}</span></li>)}</ul>
      <h4>Não inclui</h4><ul className="team-list compact">{plan.nao_inclui.map(line => <li key={line}><span>{line}</span></li>)}</ul>
      <p className="hint">Identificação desta pré-visualização: <code>{plan.plan_hash.slice(0, 8)}…{plan.plan_hash.slice(-6)}</code>. Se mudar o modelo ou o nome, confira de novo.</p>
      <div className="team-actions"><button type="button" className="primary" disabled={!canCreate(plan, templateId, name, busy)} onClick={create}>Criar equipe</button></div>
    </article>}
  </section>;
}

/** Read-only check of the installation: what is fine, what needs attention and what to do about it. */
export function DoctorSection() {
  const [report, setReport] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError('');
    try {setReport(await api.get('/doctor'));} catch (e) {setError(describeError(e));} finally {setBusy(false);}
  };
  return <section aria-labelledby="doctor-title">
    <h2 id="doctor-title">Diagnóstico da instalação</h2>
    <p className="hint">Confere dependências, pasta de dados, banco e migrações, backups, cofre, o servidor do modelo e o acesso remoto. Só lê; não altera nada.</p>
    <div className="team-actions"><button type="button" disabled={busy} onClick={run}>{report ? 'Conferir de novo' : 'Conferir agora'}</button></div>
    <Notice error={error}/>
    {report && <>
      <p role="status"><strong>{doctorHeadline(report)}</strong></p>
      <ul className="team-list">{sortedChecks(report).map(item => <li key={item.id}><div>
        <strong>{item.titulo}</strong> <span className={`badge phase-${STATE_CLASS[item.estado]}`}>{STATE_LABEL[item.estado]}</span>
        <p>{item.detalhe}</p>{item.acao && item.estado !== 'ok' && <p className="hint">O que fazer: {item.acao}</p>}</div></li>)}</ul></>}
  </section>;
}
