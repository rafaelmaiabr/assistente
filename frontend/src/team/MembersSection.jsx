import React, {useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';
import {ROLES, ROLE_HELP, ROLE_LABEL, scopeLabel} from './authModel.js';
import {Notice, useList} from './shared.jsx';

function DepartmentPicker({departments, value, onChange, disabled}) {
  const toggle = id => onChange(value.includes(id) ? value.filter(d => d !== id) : [...value, id]);
  return <fieldset className="department-picker" disabled={disabled}>
    <legend>Departamentos (nenhum marcado = a empresa inteira)</legend>
    {departments.map(d => <label key={d.id} className="check"><input type="checkbox" checked={value.includes(d.id)} onChange={() => toggle(d.id)}/> {d.name}</label>)}
    {!departments.length && <small className="hint">A empresa ainda não tem departamentos.</small>}
  </fieldset>;
}

/** Who can sign in to this company, with which role and for which departments. The sign-in itself is Google's. */
export function MembersSection({base}) {
  const {data, error, reload} = useList(`${base}/members`);
  const {data: departments} = useList(`${base}/departments`);
  const members = data.members || [];
  const [form, setForm] = useState({email: '', name: '', role: 'atendente', departments: []}), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(null);
  const run = async (action, ok) => {
    setBusy(true); setMessage({});
    try {await action(); setMessage({ok}); await reload();} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}
  };
  return <section aria-labelledby="members-title">
    <h2 id="members-title">Membros</h2>
    <p className="hint">Quem entra com a conta Google cadastrada aqui. Cadastrar não dá acesso por si só: a pessoa precisa entrar com essa conta.</p>
    <Notice error={error || message.error} ok={message.ok}/>
    <ul className="team-list">
      {members.map(m => <li key={m.user_id}><div><strong>{m.name || m.email}</strong> <span className={`badge ${m.status === 'ativo' ? '' : 'off'}`}>{m.status}</span>
        <p>{m.email} · {m.signed_in_before ? `último acesso ${m.last_login_at || '—'}` : 'ainda não entrou'}</p>
        <p>Alcance: {scopeLabel(m, departments)}</p>
        {editing?.id === m.user_id && <>
          <DepartmentPicker departments={departments} value={editing.value} disabled={busy} onChange={value => setEditing({id: m.user_id, value})}/>
          <div className="team-actions">
            <button disabled={busy} onClick={() => run(async () => {await api.patch(`${base}/members/${m.user_id}`, {departments: editing.value}); setEditing(null);}, 'Alcance atualizado.')}>Salvar alcance</button>
            <button disabled={busy} onClick={() => setEditing(null)}>Cancelar</button></div></>}</div>
        <div className="team-actions">
          <select aria-label={`Papel de ${m.email}`} value={m.role} disabled={busy} onChange={e => run(() => api.patch(`${base}/members/${m.user_id}`, {role: e.target.value}), 'Papel alterado.')}>
            {ROLES.map(r => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select>
          {m.role !== 'admin' && editing?.id !== m.user_id && <button disabled={busy} onClick={() => setEditing({id: m.user_id, value: m.departments || []})}>Limitar por departamento</button>}
          <button disabled={busy} onClick={() => run(() => api.patch(`${base}/members/${m.user_id}`, {status: m.status === 'ativo' ? 'inativo' : 'ativo'}),
            m.status === 'ativo' ? 'Acesso removido desta empresa.' : 'Acesso restaurado.')}>{m.status === 'ativo' ? 'Remover acesso' : 'Restaurar'}</button></div></li>)}
      {!members.length && <li className="team-empty">Nenhum membro cadastrado.</li>}
    </ul>
    <form className="team-form" onSubmit={e => {e.preventDefault(); run(async () => {
      await api.post(`${base}/members`, {email: form.email, role: form.role, ...(form.name.trim() ? {name: form.name} : {}),
        ...(form.role !== 'admin' && form.departments.length ? {departments: form.departments} : {})});
      setForm({email: '', name: '', role: form.role, departments: []});}, 'Membro cadastrado.');}}>
      <h3>Cadastrar membro</h3>
      <label>Email da conta Google<input type="email" required maxLength={254} value={form.email} onChange={e => setForm({...form, email: e.target.value})}/></label>
      <label>Nome (opcional)<input maxLength={80} value={form.name} onChange={e => setForm({...form, name: e.target.value})}/></label>
      <label>Papel<select value={form.role} onChange={e => setForm({...form, role: e.target.value})}>{ROLES.map(r => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select></label>
      <p className="hint">{ROLE_HELP[form.role]}</p>
      {form.role !== 'admin' && <DepartmentPicker departments={departments} value={form.departments} disabled={busy} onChange={value => setForm({...form, departments: value})}/>}
      <button type="submit" disabled={busy}>Cadastrar</button>
    </form>
  </section>;
}
