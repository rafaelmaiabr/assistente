import React from 'react';
import {decisionMessage} from './moveModel.js';

/** The exact list of messages to move. Only these buttons decide; approving sends the hash of what is shown here. */
export function MoveCards({moves, send}) {
  return <>
    {moves.proposals.map(proposal => {
      const busy = Boolean(moves.status[proposal.id]);
      return <section key={proposal.id} className="mail-card" aria-label="Mover mensagens">
        <strong>Mover {proposal.itens.length} mensagem(ns) para {proposal.destino_pasta}</strong>
        <p>De {proposal.pasta} · você vê a lista exata e só o que está aqui será movido. Nada é apagado de vez.</p>
        <ul className="memory-list">{proposal.itens.map(item =>
          <li key={item.uid}><span>uid {item.uid} · {item.de} · {item.assunto || '(sem assunto)'}</span></li>)}</ul>
        <small>Conferência: {proposal.hash.slice(0, 12)}…</small>
        <div className="mail-actions">
          <button className="primary" disabled={busy} onClick={() => send(decisionMessage(proposal, 'approve'))}>Aprovar e mover</button>
          <button disabled={busy} onClick={() => send(decisionMessage(proposal, 'discard'))}>Cancelar</button>
        </div>
        {busy && <small role="status">{moves.status[proposal.id]}</small>}
        <small>Se a mensagem mudou no servidor desde a proposta, o lote para sozinho e nada é repetido.</small>
      </section>;
    })}
    {Object.entries(moves.status).filter(([id]) => !moves.proposals.some(item => item.id === id)).map(([id, text]) =>
      <section key={id} className="mail-card ok" role="status"><strong>Movimentação</strong><p>{text}</p></section>)}
  </>;
}
