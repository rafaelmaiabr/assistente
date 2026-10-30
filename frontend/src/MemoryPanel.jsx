import React, {useState} from 'react';
import {addMessage, decisionMessage, deleteMessage, describeProposal, editMessage, MAX_ENTRY} from './memoryModel.js';

/** What the chat proposes to remember or forget. Only these buttons decide; the model has no way to press them. */
export function MemoryProposals({memory, send}) {
  return memory.pending.map(proposal => <section key={proposal.id} className="mail-card" aria-label="Proposta de memória">
    <strong>O assistente propõe uma anotação</strong>
    <p>{describeProposal(proposal)}</p>
    <div className="mail-actions">
      <button className="primary" onClick={() => send(decisionMessage(proposal.id, 'approve'))}>Aprovar</button>
      <button onClick={() => send(decisionMessage(proposal.id, 'discard'))}>Recusar</button>
    </div>
    <small>Nada é guardado sem este clique. O que você aprovar vale a partir da próxima conversa.</small>
  </section>);
}

/** The person's memory: see, add, edit and delete. Stored only on this computer. */
export function MemoryPanel({memory, send}) {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(null);
  const [text, setText] = useState('');
  const act = message => {if (message) send(message);};
  return <details className="persistence"><summary>Memória</summary><div className="device-options">
    <small>Anotações curtas que o assistente usa nas próximas conversas. Ficam só neste computador. {memory.used}/{memory.limit} caracteres.</small>
    {memory.entries.length === 0 && <small>Nada anotado ainda.</small>}
    <ul className="memory-list">{memory.entries.map(item => <li key={item.id}>
      {editing === item.id
        ? <><input aria-label="Editar anotação" value={text} maxLength={MAX_ENTRY} onChange={e => setText(e.target.value)}/>
            <button onClick={() => {act(editMessage(item.id, text)); setEditing(null);}}>Salvar</button>
            <button onClick={() => setEditing(null)}>Cancelar</button></>
        : <><span>{item.text}</span>
            <button onClick={() => {setEditing(item.id); setText(item.text);}}>Editar</button>
            <button onClick={() => act(deleteMessage(item.id))}>Apagar</button></>}
    </li>)}</ul>
    <label>Nova anotação<input value={draft} maxLength={MAX_ENTRY} onChange={e => setDraft(e.target.value)} placeholder="Ex.: prefere ser chamado de Rafael"/></label>
    <button disabled={!addMessage(draft)} onClick={() => {act(addMessage(draft)); setDraft('');}}>Adicionar</button>
    {memory.message && <small role="status">{memory.message}</small>}
  </div></details>;
}
