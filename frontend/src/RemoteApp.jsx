import React, {useEffect, useState} from 'react';
import {setCsrf} from './team/api.js';
import {TeamPanel} from './team/TeamPanel.jsx';
import {loginErrorFrom, loginMessage, sessionKind} from './team/authModel.js';

/** Asks the server how this page is served: the local application (no sign-in) or the remote one (Google sign-in). */
export async function readSession() {
  try {
    const response = await fetch('/auth/session', {headers: {Accept: 'application/json'}});
    const data = await response.json().catch(() => null);
    return {kind: sessionKind(response.status, data), data};
  } catch {return {kind: 'unreachable', data: null};}
}

export function LoginScreen({configured = true}) {
  const message = loginMessage(loginErrorFrom(window.location.search));
  return <main className="login" aria-labelledby="login-title">
    <h1 id="login-title">Assistente</h1>
    <p>Entre com sua conta Google para acessar a equipe.</p>
    {message && <p className="team-error" role="alert">{message}</p>}
    {!configured && <p className="team-error" role="alert">{loginMessage('nao_configurado')}</p>}
    <a className="primary" href="/auth/google/start" role="button">Entrar com Google</a>
    <small className="hint">Só entram pessoas cadastradas por um administrador da empresa.</small>
  </main>;
}

export function RemoteApp({session}) {
  const [signedOut, setSignedOut] = useState(false);
  setCsrf(session.csrf);  // before any child asks the server for anything
  useEffect(() => {setCsrf(session.csrf);}, [session.csrf]);
  const leave = async () => {
    try {
      await fetch('/auth/logout', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf}, body: '{}'});
    } catch { /* the server clears the cookie when it answers; if it cannot be reached, the session simply expires */ }
    setCsrf(''); setSignedOut(true);
    window.location.assign('/');
  };
  if (signedOut) return null;
  return <div className="remote-shell">
    <div className="remote-bar"><span>{session.user.name || session.user.email}</span><small>{session.user.email}</small>
      <button type="button" onClick={leave}>Sair</button></div>
    <TeamPanel remote focus={null}/>
  </div>;
}
