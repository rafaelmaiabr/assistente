import React, {useState} from 'react';
import {DRAFT_LABEL, SKILL_LABEL, isOpen, providerHint, validSetup} from './mailModel.js';

/** Which skill is in play, like a chip: "E-mail · ativa". Clicking an unconfigured one opens the secure form. */
export function SkillChip({state, onConfigure}) {
  if (!state) return null;
  const label = SKILL_LABEL[state] || state;
  return state === 'configurar'
    ? <button className="skill-chip" onClick={onConfigure} title="Conectar sua caixa de email">{label}</button>
    : <span className={`skill-chip ${state}`} role="status">{label}</span>;
}

/** The secure form. The password field is only ever read here and sent once, straight to the local server. */
export function SetupCard({setup, onSubmit, onClose}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [imapHost, setImapHost] = useState('');
  const [smtpHost, setSmtpHost] = useState('');
  const [readOnly, setReadOnly] = useState(false);
  if (!setup) return null;
  if (setup.done) return <section className="mail-card ok" aria-live="polite"><strong>{setup.done}</strong><div className="mail-actions"><button onClick={onClose}>Fechar</button></div></section>;
  const ready = validSetup({email, password}) && !setup.busy;
  const submit = event => {
    event.preventDefault();
    if (!ready) return;
    onSubmit({email, password, imapHost, smtpHost, readOnly});
    setPassword('');  // the secret does not stay in the page state after it was sent
  };
  return <form className="mail-card" onSubmit={submit} autoComplete="off" aria-label="Configurar caixa de email">
    <strong>Configurar caixa de email</strong>
    <p>A senha vai direto desta tela para o cofre do Windows. Ela não passa pela conversa nem pelo modelo.</p>
    <label>Seu email<input type="email" value={email} onChange={e => setEmail(e.target.value)} autoComplete="off" placeholder="voce@exemplo.com" disabled={setup.busy}/></label>
    <label>Senha de aplicativo<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="new-password" disabled={setup.busy}/></label>
    {providerHint(email) && <small>{providerHint(email)}</small>}
    <details><summary>Avançado</summary>
      <label>Servidor de leitura (IMAP)<input value={imapHost} onChange={e => setImapHost(e.target.value)} placeholder="automático" disabled={setup.busy}/></label>
      <label>Servidor de envio (SMTP)<input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} placeholder="automático" disabled={setup.busy}/></label>
      <label className="check"><input type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} disabled={setup.busy}/>Somente leitura (não preparar respostas)</label>
    </details>
    {setup.error && <div className="mail-error" role="alert">{setup.error}</div>}
    <div className="mail-actions">
      <button type="submit" className="primary" disabled={!ready}>{setup.busy ? 'Testando a conexão…' : 'Conectar'}</button>
      <button type="button" onClick={onClose} disabled={setup.busy}>Cancelar</button>
    </div>
  </form>;
}

/** The exact draft. Approving sends the hash of what is shown here; the model has no way to press this button. */
export function DraftCard({draft, onApprove, onDiscard}) {
  const open = isOpen(draft);
  return <section className={`mail-card draft ${draft.state}`} aria-label="Rascunho de resposta">
    <strong>Rascunho de resposta · {DRAFT_LABEL[draft.state] || draft.state}</strong>
    <dl><dt>Para</dt><dd>{draft.to}</dd><dt>Assunto</dt><dd>{draft.subject}</dd></dl>
    <pre className="draft-body">{draft.body}</pre>
    {draft.detail && <small>{draft.detail}</small>}
    {open && <div className="mail-actions">
      <button className="primary" onClick={() => onApprove(draft)}>Aprovar e enviar</button>
      <button onClick={() => onDiscard(draft)}>Descartar</button>
    </div>}
    {open && <small>Nada é enviado sem este clique. O rascunho expira em 1 hora.</small>}
  </section>;
}
