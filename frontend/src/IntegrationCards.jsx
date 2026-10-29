import React, {useEffect, useRef, useState} from 'react';
import {blankValues, editValues, validateForm} from './team/connectorsModel.js';
import {cancelMessage, cardKey, cardNotice, mailPickMessage, oauthMessage, openMessage, pickMessage, rowActions, rowText, safeAuthUrl, submitMessage, useMessage} from './integrationModel.js';

/** The secure form of one driver, built from its declared fields. Secrets are password inputs, sent once and cleared. */
function IntegrationForm({form, status, send}) {
  const [values, setValues] = useState(editValues(form.fields, form.values));
  const [label, setLabel] = useState(form.label);
  const [problem, setProblem] = useState('');
  const element = useRef(null);
  // The person just asked for this form: keyboard and screen-reader users land on its first field.
  useEffect(() => {element.current?.scrollIntoView({block: 'nearest'}); element.current?.querySelector('input')?.focus();}, []);
  const set = (name, value) => setValues(current => ({...current, [name]: value}));
  const secretNames = form.fields.filter(field => field.kind === 'secret').map(field => field.name);
  const busy = Boolean(status?.busy);
  const submit = event => {
    event.preventDefault();
    const found = validateForm(form.fields, values, {editing: form.editing});
    setProblem(found);
    if (found || busy) return;
    send(submitMessage(form, label, values));
    setValues(current => ({...current, ...Object.fromEntries(secretNames.map(name => [name, '']))}));  // the secret does not stay in the page
  };
  const authorized = status?.next === 'oauth';
  return <form ref={element} className="mail-card" onSubmit={submit} autoComplete="off" aria-label={`Conectar ${form.label}`}>
    <strong>{form.editing ? `Reconfigurar “${form.connection_label}”` : `Conectar ${form.label}`} · {form.company}</strong>
    <p>{form.auth === 'oauth' ? 'Os dados abaixo não incluem senha: o acesso é autorizado no navegador.' : 'A senha vai direto desta tela para o cofre do Windows. Ela não passa pela conversa nem pelo modelo.'}</p>
    {form.auth === 'oauth' && form.steps.length > 0 && <ol className="integration-steps">{form.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>}
    {!form.editing && <label>Nome da conexão<input required maxLength={120} value={label} onChange={e => setLabel(e.target.value)} disabled={busy}/></label>}
    {form.fields.map(field => <label key={field.name}>{field.label}{field.required === false || (field.kind === 'secret' && form.editing) ? ' (opcional)' : ''}
      <input name={field.name} type={field.kind === 'secret' ? 'password' : field.kind === 'url' ? 'url' : 'text'} inputMode={field.kind === 'number' ? 'numeric' : undefined}
        autoComplete={field.kind === 'secret' ? 'new-password' : 'off'} spellCheck="false" value={values[field.name]} disabled={busy}
        onChange={e => set(field.name, e.target.value)}/>
      {field.help && <small>{field.help}</small>}
      {field.kind === 'secret' && form.editing && <small>Deixe em branco para manter a senha atual.</small>}</label>)}
    {problem && <div className="mail-error" role="alert">{problem}</div>}
    {status?.message && <div className={status.ok ? 'mail-ok' : 'mail-error'} role={status.ok ? 'status' : 'alert'}>{status.message}</div>}
    {status?.busy && <small role="status">{status.text}</small>}
    {status?.waiting && status.url && safeAuthUrl(status.url) && <p><a href={status.url} target="_blank" rel="noopener noreferrer">Abrir a página de autorização</a></p>}
    <div className="mail-actions">
      {!authorized && !status?.waiting && <button type="submit" className="primary" disabled={busy}>{busy ? 'Aguarde…' : form.editing ? 'Salvar e testar' : 'Conectar e testar'}</button>}
      {authorized && !status?.waiting && <button type="button" className="primary" onClick={() => send(oauthMessage(form))}>Autorizar no navegador</button>}
      {status?.waiting && <button type="button" onClick={() => send(cancelMessage(form.request_id))}>Cancelar autorização</button>}
    </div>
  </form>;
}

function IntegrationCard({card, form, status, send}) {
  const notice = cardNotice(card);
  const act = (row, id) => {
    if (id === 'use') send(useMessage(card, row));
    else if (id === 'reconfigure' || id === 'connect' || id === 'reconnect') send(openMessage(card, row));
  };
  return <section className="mail-card" aria-label={card.label ? `Integração: ${card.label}` : 'Integração'}>
    <strong>{card.state === 'caixa' ? 'Escolher a caixa de email' : card.label ? `Conectar ${card.label}` : 'Integração'}</strong>
    {notice && <p>{notice}</p>}
    {card.state === 'caixa' && <div className="mail-actions">{card.options.map(option => <button key={option.id} disabled={Boolean(status?.busy)}
      className={option.label === card.current ? 'primary' : ''} aria-pressed={option.label === card.current} onClick={() => send(mailPickMessage(card, option))}>{option.label}</button>)}</div>}
    {card.state === 'escolher' && <div className="mail-actions">{card.options.map(option => <button key={option.provider} onClick={() => send(pickMessage(card, option.provider))}>{option.label}</button>)}</div>}
    {card.notice && <p className="connector-note" role="note">{card.notice}</p>}
    {card.state === 'pronto' && !form && <ul className="team-list compact">{card.rows.map(row => <li key={row.company_id} className="stack">
      <span><strong>{row.company}</strong>: {rowText(row)}</span>
      <div className="mail-actions">{rowActions(row).map(action => <button key={action.id} className={action.id === 'connect' || action.id === 'reconnect' ? 'primary' : ''}
        disabled={Boolean(status?.busy)} onClick={() => act(row, action.id)}>{action.label}</button>)}</div></li>)}</ul>}
    {!form && status?.message && <div className={status.ok ? 'mail-ok' : 'mail-error'} role={status.ok ? 'status' : 'alert'}>{status.message}</div>}
    {form && <IntegrationForm key={`${form.request_id}-${form.company_id}`} form={form} status={status} send={send}/>}
  </section>;
}

export function IntegrationCards({integrations, send}) {
  return <>
    {integrations.cards.map(card => <IntegrationCard key={cardKey(card)} card={card} form={integrations.forms[cardKey(card)]}
      status={integrations.status[cardKey(card)]} send={send}/>)}
    {integrations.status.geral?.message && <div className="mail-error" role="alert">{integrations.status.geral.message}</div>}
  </>;
}
