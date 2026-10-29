import React, {useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';
import {Notice, useList} from './shared.jsx';
import {auditLine, blankValues, codeLabel, connectionState, driverNotice, editValues, formPayload, lastTestLine, validateForm} from './connectorsModel.js';

/** A form built from the driver's own declared fields. Secrets are password inputs that are cleared after every attempt. */
function ConnectorForm({driver, connection, base, onDone, onCancel}) {
  const editing = Boolean(connection);
  const [values, setValues] = useState(editing ? editValues(driver.fields, connection.values) : blankValues(driver.fields));
  const [label, setLabel] = useState(editing ? connection.label : driver.provider);
  const [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const set = (name, value) => setValues(current => ({...current, [name]: value}));
  const submit = async e => {
    e.preventDefault();
    const problem = validateForm(driver.fields, values, {editing});
    if (problem) {setMessage({error: problem}); return;}
    setBusy(true); setMessage({});
    try {
      const body = {values: formPayload(driver.fields, values)};
      if (editing) await api.put(`${base}/connectors/connections/${connection.id}`, body);
      else await api.post(`${base}/connectors/${driver.provider}/connections`, {label, ...body});
      setValues(blankValues(driver.fields));  // never keep what was typed after an attempt
      await onDone(editing ? 'Configuração salva. Use "Testar conexão" para conferir.' : 'Conexão criada. Use "Testar conexão" para conferir.');
    } catch (error) {
      setValues(current => ({...current, ...Object.fromEntries(driver.fields.filter(f => f.kind === 'secret').map(f => [f.name, '']))}));
      setMessage({error: describeError(error)});
    } finally {setBusy(false);}
  };
  return <form className="team-form" onSubmit={submit} autoComplete="off" aria-label={editing ? `Configurar ${connection.label}` : `Conectar ${driver.provider}`}>
    <h4>{editing ? `Configurar “${connection.label}”` : `Nova conexão: ${driver.provider}`}</h4>
    {!editing && <label>Nome da conexão<input required maxLength={120} value={label} onChange={e => setLabel(e.target.value)}/></label>}
    {driver.fields.map(field => <label key={field.name}>{field.label}{field.required === false || (field.kind === 'secret' && editing) ? ' (opcional)' : ''}
      <input name={field.name} type={field.kind === 'secret' ? 'password' : field.kind === 'number' ? 'text' : field.kind === 'url' ? 'url' : 'text'}
        inputMode={field.kind === 'number' ? 'numeric' : undefined} autoComplete={field.kind === 'secret' ? 'new-password' : 'off'} spellCheck="false"
        value={values[field.name]} onChange={e => set(field.name, e.target.value)}
        aria-describedby={field.help ? `help-${driver.provider}-${field.name}` : undefined}/>
      {field.help && <span className="hint" id={`help-${driver.provider}-${field.name}`}>{field.help}</span>}
      {field.kind === 'secret' && editing && <span className="hint">Deixe em branco para manter a senha atual. O valor nunca é exibido.</span>}</label>)}
    <Notice error={message.error}/>
    <div className="team-actions"><button type="submit" disabled={busy}>{editing ? 'Salvar configuração' : 'Conectar'}</button>
      <button type="button" disabled={busy} onClick={onCancel}>Cancelar</button></div>
  </form>;
}

function ConnectionRow({driver, connection, base, onChanged, onEdit, remote}) {
  const [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const state = connectionState(connection, driver.auth);
  const test = async () => {
    setBusy(true); setMessage({});
    try {
      const result = await api.post(`${base}/connectors/connections/${connection.id}/test`);
      await onChanged();
      setMessage(result.ok ? {ok: codeLabel('ok')} : {error: result.mensagem || codeLabel(result.erro)});
    } catch (error) {setMessage({error: describeError(error)});} finally {setBusy(false);}
  };
  return <li className="stack">
    <div className="connector-head"><strong>{connection.label}</strong> <span className={`badge ${state.tone === 'ok' ? '' : state.tone}`}>{state.text}</span></div>
    {connection.values && <p>{Object.entries(connection.values).map(([k, v]) => `${k}: ${v}`).join(' · ')}</p>}
    <p>{lastTestLine(connection)}</p>
    <div className="team-actions">
      {driver.configurable && !remote && <button disabled={busy || !connection.configured || (driver.auth !== 'none' && !connection.credential)} onClick={test}>Testar conexão</button>}
      {driver.configurable && !remote && <button disabled={busy} onClick={() => onEdit(connection)}>Configurar</button>}</div>
    <Notice error={message.error} ok={message.ok}/>
  </li>;
}

function Driver({driver, base, onChanged, remote}) {
  const [form, setForm] = useState(null), [message, setMessage] = useState({});
  const notice = driverNotice(driver);
  const done = async ok => {setForm(null); setMessage({ok}); await onChanged();};
  return <li className="stack">
    <div className="connector-head"><strong>{driver.provider}</strong> <span className={`badge ${driver.mcp ? '' : 'warn'}`}>{driver.mcp ? 'MCP' : `${driver.protocol.toUpperCase()} (não MCP)`}</span> <span className="hint">versão {driver.version}</span></div>
    {notice && <p className="connector-note" role="note">{notice}</p>}
    <ul className="team-list compact">
      {driver.connections.map(c => <ConnectionRow key={c.id} driver={driver} connection={c} base={base} onChanged={onChanged} remote={remote}
        onEdit={connection => {setMessage({}); setForm({connection});}}/>)}
      {!driver.connections.length && <li className="team-empty">Nenhuma conexão deste conector para esta empresa.</li>}</ul>
    {form ? <ConnectorForm driver={driver} connection={form.connection} base={base} onDone={done} onCancel={() => setForm(null)}/>
      : remote ? null : driver.configurable ? <div className="team-actions"><button onClick={() => {setMessage({}); setForm({});}}>Conectar {driver.provider}</button></div>
        : <p className="hint">Este conector ainda não pode ser configurado pelo painel.</p>}
    <Notice ok={message.ok}/>
  </li>;
}

export function ConnectorsSection({base, remote = false}) {
  const overview = useList(`${base}/connectors`), audit = useList(`${base}/connectors/audit`);
  const [showAudit, setShowAudit] = useState(false);
  const reload = async () => {await overview.reload(); await audit.reload();};
  return <section aria-labelledby="connectors-title">
    <h2 id="connectors-title">Conectores</h2>
    <p className="hint">Conectores ligam a equipe a sistemas externos. O protocolo MCP é o preferido; quando um conector não usa MCP, o motivo aparece aqui. Senhas vão direto ao cofre do Windows: nunca são exibidas nem voltam do servidor.</p>
    {remote && <p className="connector-note" role="note">Conectar, configurar e testar só é possível no computador onde o assistente está instalado. Aqui você vê o estado.</p>}
    <Notice error={overview.error}/>
    {overview.data.map(capability => <section key={capability.capability} aria-label={capability.label}>
      <h3>{capability.label}</h3>
      <ul className="team-list">
        {capability.drivers.map(d => <Driver key={d.provider} driver={d} base={base} onChanged={reload} remote={remote}/>)}
        {!capability.drivers.length && <li className="team-empty">Nenhum conector instalado para esta capacidade ainda.</li>}</ul>
    </section>)}
    <div className="team-actions"><button type="button" aria-expanded={showAudit} onClick={() => setShowAudit(!showAudit)}>{showAudit ? 'Ocultar' : 'Mostrar'} histórico de configuração</button></div>
    {showAudit && <ul className="team-list compact" aria-label="Histórico de configuração">
      {audit.data.map((entry, index) => <li key={index}><span>{auditLine(entry)}</span></li>)}
      {!audit.data.length && <li className="team-empty">Nenhuma alteração registrada.</li>}</ul>}
  </section>;
}
