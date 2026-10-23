import React, {useState} from 'react';
import {api} from './api.js';
import {commaList, credentialLabel, describeError, secretIsAcceptable} from './model.js';
import {Notice, useList} from './shared.jsx';

const KINDS = [['mcp', 'Servidor MCP aprovado'], ['imap', 'Email (IMAP)'], ['smtp', 'Email (SMTP)'], ['http', 'HTTP'], ['outro', 'Outro']];

function Credential({base, connection, onChanged}) {
  const [secret, setSecret] = useState(''), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, ok) => {
    setBusy(true); setMessage({});
    try {await action(); setSecret(''); setMessage({ok}); await onChanged();}
    catch (e) {setSecret(''); setMessage({error: describeError(e)});}  // never keep what was typed after an attempt
    finally {setBusy(false);}
  };
  const url = `${base}/connections/${connection.id}`;
  const legacy = connection.credential?.store === 'elevenlabs-legado';
  return <div className="team-credential">
    <p><strong>{credentialLabel(connection)}</strong>. O valor nunca é exibido nem volta do servidor.</p>
    {legacy ? <p className="hint">Esta chave é gerenciada nas configurações de voz.</p> :
      <form onSubmit={e => {e.preventDefault(); void run(() => api.put(`${url}/credential`, {secret}), 'Credencial salva no cofre do Windows.');}} autoComplete="off">
        <label>{connection.credential ? 'Substituir credencial' : 'Salvar credencial'}
          <input type="password" autoComplete="new-password" spellCheck="false" value={secret} onChange={e => setSecret(e.target.value)}/></label>
        <button type="submit" disabled={busy || !secretIsAcceptable(secret)}>Salvar no cofre</button></form>}
    {connection.credential && <div className="team-actions">
      {connection.credential.state === 'ativa' && <button disabled={busy} onClick={() => run(() => api.post(`${url}/credential/revoke`), 'Credencial revogada: novas chamadas ficam bloqueadas.')}>Revogar</button>}
      {!legacy && <button disabled={busy} onClick={() => run(() => api.del(`${url}/credential`), 'Credencial removida.')}>Remover</button>}</div>}
    <Notice error={message.error} ok={message.ok}/>
  </div>;
}

function Catalog({base, entries, onChanged}) {
  const [form, setForm] = useState({catalog_key: '', entry_version: '1.0.0', url: '', tools: ''}), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, ok) => {setBusy(true); setMessage({}); try {await action(); setMessage({ok}); await onChanged();} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}};
  return <section aria-labelledby="catalog-title"><h3 id="catalog-title">Servidores MCP aprovados</h3>
    <p className="hint">Somente servidores deste catálogo podem ser usados por conexões MCP. Comandos locais (stdio) são cadastrados fora do painel.</p>
    <ul className="team-list compact">{entries.map(e => <li key={e.id}><span><strong>{e.catalog_key}</strong> {e.entry_version} · {e.transport} · {e.url || 'comando local'} <span className={`badge ${e.status === 'ativo' ? '' : 'off'}`}>{e.status}</span></span>
      <button disabled={busy} onClick={() => run(() => api.patch(`${base}/mcp-catalog/${e.id}`, {status: e.status === 'ativo' ? 'inativo' : 'ativo', expected_revision: e.revision}), 'Catálogo atualizado.')}>{e.status === 'ativo' ? 'Desativar' : 'Ativar'}</button></li>)}
      {!entries.length && <li className="team-empty">Nenhum servidor aprovado.</li>}</ul>
    <form className="team-form" onSubmit={e => {e.preventDefault(); void run(() => api.post(`${base}/mcp-catalog`, {catalog_key: form.catalog_key, entry_version: form.entry_version, transport: 'http', url: form.url, tools: commaList(form.tools)}), 'Servidor aprovado. As ferramentas ainda precisam ser classificadas para ficarem disponíveis.');}}>
      <h4>Aprovar um servidor HTTP</h4>
      <div className="team-row"><label>Identificador<input required value={form.catalog_key} onChange={e => setForm({...form, catalog_key: e.target.value})} placeholder="ex.: erp-demo"/></label>
        <label>Versão<input required value={form.entry_version} onChange={e => setForm({...form, entry_version: e.target.value})}/></label></div>
      <label>Endereço (URL)<input required type="url" value={form.url} onChange={e => setForm({...form, url: e.target.value})} placeholder="http://127.0.0.1:9000/mcp"/></label>
      <label>Ferramentas declaradas (vírgula)<input value={form.tools} onChange={e => setForm({...form, tools: e.target.value})}/></label>
      <Notice error={message.error} ok={message.ok}/>
      <button type="submit" disabled={busy}>Aprovar servidor</button></form>
  </section>;
}

export function ConnectionsSection({base}) {
  const connections = useList(`${base}/connections`), catalog = useList(`${base}/mcp-catalog`);
  const [form, setForm] = useState({label: '', provider: '', connection_kind: 'imap', catalog_entry_id: '', scopes: ''});
  const [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, ok) => {setBusy(true); setMessage({}); try {await action(); setMessage({ok}); await connections.reload();} catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}};
  const create = () => run(async () => {
    const body = {label: form.label, provider: form.provider, connection_kind: form.connection_kind, scopes: commaList(form.scopes)};
    if (form.connection_kind === 'mcp') body.catalog_entry_id = form.catalog_entry_id;
    await api.post(`${base}/connections`, body); setForm({...form, label: '', provider: '', scopes: ''});
  }, 'Conexão criada sem credencial. Salve a credencial quando quiser usá-la.');
  return <section aria-labelledby="conn-title">
    <h2 id="conn-title">Conexões</h2>
    <p className="hint">Mostram apenas estado e escopos. Nada é conectado ou contratado automaticamente.</p>
    <Notice error={connections.error || message.error} ok={message.ok}/>
    <ul className="team-list">{connections.data.map(c => <li key={c.id} className="stack"><div><strong>{c.label}</strong> <span className="badge">{c.connection_kind}</span>
      <span className={`badge ${c.status === 'ativo' ? '' : 'off'}`}>{c.status}</span>
      <p>Provedor: {c.provider}{c.endpoint ? ` · ${c.endpoint}` : ''} · escopos: {c.scopes.length ? c.scopes.join(', ') : 'nenhum'}</p></div>
      <Credential base={base} connection={c} onChanged={connections.reload}/>
      <button disabled={busy} onClick={() => run(() => api.patch(`${base}/connections/${c.id}`, {status: c.status === 'ativo' ? 'inativo' : 'ativo', expected_revision: c.revision}),
        c.status === 'ativo' ? 'Conexão desativada.' : 'Conexão reativada.')}>{c.status === 'ativo' ? 'Desativar conexão' : 'Reativar conexão'}</button></li>)}
      {!connections.data.length && <li className="team-empty">Nenhuma conexão cadastrada.</li>}</ul>
    <form className="team-form" onSubmit={e => {e.preventDefault(); void create();}}>
      <h3>Nova conexão</h3>
      <label>Nome<input required maxLength={120} value={form.label} onChange={e => setForm({...form, label: e.target.value})}/></label>
      <div className="team-row"><label>Tipo<select value={form.connection_kind} onChange={e => setForm({...form, connection_kind: e.target.value})}>{KINDS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
        <label>Provedor (identificador)<input required pattern="[a-z0-9][a-z0-9._-]*" value={form.provider} onChange={e => setForm({...form, provider: e.target.value})} placeholder="ex.: erp-demo"/></label></div>
      {form.connection_kind === 'mcp' && <label>Servidor aprovado<select required value={form.catalog_entry_id} onChange={e => setForm({...form, catalog_entry_id: e.target.value})}>
        <option value="">Selecione</option>{catalog.data.filter(e => e.status === 'ativo').map(e => <option key={e.id} value={e.id}>{e.catalog_key} {e.entry_version}</option>)}</select></label>}
      <label>Escopos (vírgula)<input value={form.scopes} onChange={e => setForm({...form, scopes: e.target.value})} placeholder="ex.: cobrancas.read"/></label>
      <button type="submit" disabled={busy}>Criar conexão</button></form>
    <Catalog base={base} entries={catalog.data} onChanged={catalog.reload}/>
  </section>;
}
