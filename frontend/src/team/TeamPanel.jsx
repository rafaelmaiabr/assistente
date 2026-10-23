import React, {useCallback, useEffect, useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';
import {Notice, useList} from './shared.jsx';
import {PersistenceSettings} from '../PersistenceSettings.jsx';
import {AgentsSection} from './AgentsSection.jsx';
import {SkillsSection} from './SkillsSection.jsx';
import {ConnectionsSection} from './ConnectionsSection.jsx';
import {CasesSection} from './CasesSection.jsx';
import {MembersSection} from './MembersSection.jsx';
import {DoctorSection, OnboardingSection} from './OnboardingSection.jsx';
import {ROLE_LABEL, visibleTabs} from './authModel.js';

const TABS = [['cases', 'Demandas'], ['departments', 'Departamentos'], ['agents', 'Agentes'], ['skills', 'Skills'], ['connections', 'Conexões'], ['members', 'Membros']];

function Departments({base}) {
  const {data, error, reload} = useList(`${base}/departments`);
  const [form, setForm] = useState({name: '', scope: ''}), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, ok) => {
    setBusy(true); setMessage({});
    try {await action(); setMessage({ok}); await reload();} catch (e) {setMessage({error: describeError(e)});}
    finally {setBusy(false);}
  };
  return <section aria-labelledby="dep-title">
    <h2 id="dep-title">Departamentos</h2>
    <Notice error={error || message.error} ok={message.ok}/>
    <ul className="team-list">
      {data.map(d => <li key={d.id}><div><strong>{d.name}</strong> <span className={`badge ${d.status === 'ativo' ? '' : 'off'}`}>{d.status}</span><p>{d.scope}</p></div>
        <button disabled={busy} onClick={() => run(() => api.patch(`${base}/departments/${d.id}`, {status: d.status === 'ativo' ? 'inativo' : 'ativo', expected_revision: d.revision}),
          d.status === 'ativo' ? 'Departamento desativado.' : 'Departamento reativado.')}>{d.status === 'ativo' ? 'Desativar' : 'Reativar'}</button></li>)}
      {!data.length && <li className="team-empty">Nenhum departamento ainda.</li>}
    </ul>
    <form className="team-form" onSubmit={e => {e.preventDefault(); run(async () => {await api.post(`${base}/departments`, form); setForm({name: '', scope: ''});}, 'Departamento criado.');}}>
      <h3>Novo departamento</h3>
      <label>Nome<input required maxLength={120} value={form.name} onChange={e => setForm({...form, name: e.target.value})}/></label>
      <label>O que ele cuida<input required maxLength={500} value={form.scope} onChange={e => setForm({...form, scope: e.target.value})}/></label>
      <button type="submit" disabled={busy}>Criar departamento</button>
    </form>
  </section>;
}

/** `remote` is set when the page is served by the remote listener: the person's role per company decides what is shown. */
export function TeamPanel({onOpenMenu, focus, remote = false}) {
  const [overview, setOverview] = useState(null), [error, setError] = useState(''), [companyId, setCompanyId] = useState('');
  const [tab, setTab] = useState('cases'), [name, setName] = useState(''), [busy, setBusy] = useState(false), [tool, setTool] = useState('');
  const load = useCallback(async () => {
    try {const data = await api.get('/state'); setOverview(data); setError('');
      setCompanyId(current => data.companies.some(c => c.id === current) ? current : (data.companies[0]?.id || ''));}
    catch (e) {setError(describeError(e));}
  }, []);
  useEffect(() => {void load();}, [load]);
  // The conversation or the avatar asked for one case: show that company, the cases tab and that case.
  useEffect(() => {if (focus) {setCompanyId(focus.company_id); setTab('cases');}}, [focus]);
  const company = overview?.companies.find(c => c.id === companyId);
  const base = company ? `/companies/${company.id}` : '';
  const role = remote ? company?.role : 'owner';  // the local owner is not limited by company roles
  const tabs = visibleTabs(TABS, role);
  useEffect(() => {if (company && !tabs.some(([id]) => id === tab)) setTab('cases');}, [company?.id, role]);
  const create = async e => {
    e.preventDefault(); setBusy(true);
    try {const created = await api.post('/companies', {name}); setName(''); await load(); setCompanyId(created.id); setError('');}
    catch (err) {setError(describeError(err));} finally {setBusy(false);}
  };
  return <main className="team">
    <header>
      <div className="header-left">{onOpenMenu && <button className="menu-button" aria-label="Abrir menu" onClick={onOpenMenu}>☰</button>}<span className="page-title">Equipe</span></div>
      <div className="team-company" aria-live="polite">{company ? <>Empresa: <strong>{company.name}</strong>{remote && role && <> · {ROLE_LABEL[role]}</>}</> : 'Nenhuma empresa selecionada'}</div>
    </header>
    <div className="team-body">
      {!remote && overview && !overview.enabled && <section className="team-callout" aria-label="Atendimento persistente desligado">
        <h2>Ative o atendimento persistente</h2>
        <p>A equipe fica salva neste computador e é opcional. Ative abaixo; o chat continua funcionando sem isso.</p>
        <PersistenceSettings/><button onClick={load}>Já ativei, recarregar</button>
      </section>}
      <Notice error={error}/>
      {!remote && overview && <div className="team-actions" role="group" aria-label="Ferramentas">
        {overview.enabled && <button type="button" aria-pressed={tool === 'modelo'} onClick={() => setTool(tool === 'modelo' ? '' : 'modelo')}>Configurar por um modelo</button>}
        <button type="button" aria-pressed={tool === 'diagnostico'} onClick={() => setTool(tool === 'diagnostico' ? '' : 'diagnostico')}>Diagnóstico da instalação</button></div>}
      {!remote && tool === 'modelo' && overview?.enabled && <OnboardingSection onCreated={async id => {await load(); setCompanyId(id); setTool('');}}/>}
      {!remote && tool === 'diagnostico' && <DoctorSection/>}
      {overview?.enabled && <>
        <section className="team-company-bar" aria-label="Empresa">
          <label>Empresa
            <select value={companyId} onChange={e => setCompanyId(e.target.value)}>
              {!overview.companies.length && <option value="">{remote ? 'Nenhuma empresa disponível' : 'Crie uma empresa para começar'}</option>}
              {overview.companies.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select></label>
          {!remote && <form onSubmit={create}><label>Nova empresa<input required maxLength={120} value={name} onChange={e => setName(e.target.value)} placeholder="Nome da empresa"/></label>
            <button type="submit" disabled={busy}>Criar</button></form>}
          {!remote && <p className="demo-note">Os dados de demonstração são fictícios. Nenhuma conta paga ou real é conectada automaticamente.</p>}
        </section>
        {company ? <>
          <div role="tablist" aria-label="Seções da equipe" className="team-tabs">
            {tabs.map(([id, label]) => <button key={id} role="tab" id={`tab-${id}`} aria-selected={tab === id} aria-controls={`panel-${id}`}
              className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} key={company.id + tab}>
            {tab === 'cases' && <CasesSection base={base} openRequest={focus && focus.company_id === company.id ? focus.case_id : ''}/>}
            {tab === 'departments' && <Departments base={base}/>}
            {tab === 'agents' && <AgentsSection base={base}/>}
            {tab === 'skills' && <SkillsSection base={base}/>}
            {tab === 'connections' && <ConnectionsSection base={base}/>}
            {tab === 'members' && <MembersSection base={base}/>}
          </div></> : <p className="team-empty">{remote ? 'Você ainda não participa de nenhuma empresa.' : 'Crie ou selecione uma empresa, ou use "Configurar por um modelo", para começar.'}</p>}
      </>}
    </div>
  </main>;
}
