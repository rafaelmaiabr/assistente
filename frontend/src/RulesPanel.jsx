import React, {useState} from 'react';
import {ACTIONS, STATUS_LABEL, decisionMessage, editMessage, toggleMessage} from './rulesModel.js';

/** The question for a rule the chat proposed. Only these buttons decide; the model has no way to press them. */
export function RuleProposals({rules, send}) {
  return rules.pending.map(proposal => <section key={proposal.id} className="mail-card" aria-label="Proposta de regra da caixa">
    <strong>Regra da caixa · {proposal.conta_nome}</strong>
    <p>{proposal.pergunta}</p>
    <dl><dt>Vale para</dt><dd>{proposal.exemplos?.positivos?.join(', ')}</dd><dt>Não vale para</dt><dd>{proposal.exemplos?.negativos?.join(', ')}</dd></dl>
    <div className="mail-actions">
      <button className="primary" onClick={() => send(decisionMessage(proposal.id, 'criar'))}>Criar regra</button>
      <button onClick={() => send(decisionMessage(proposal.id, 'uma_vez'))}>Só desta vez</button>
      <button onClick={() => send(decisionMessage(proposal.id, 'recusar'))}>Recusar</button>
    </div>
    <small>Nada é salvo sem a sua escolha. “Só desta vez” aplica agora e não guarda.</small>
  </section>);
}

function RuleRow({rule, send}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(rule.condicao.valor || '');
  const [extra, setExtra] = useState((rule.excecoes || []).join('; '));
  const [action, setAction] = useState(rule.acao);
  const replaced = rule.status === 'substituida';
  const save = () => {send(editMessage(rule.id, {valor: value, excecoes: extra, acao: action})); setEditing(false);};
  return <li>
    <span><strong>{STATUS_LABEL[rule.status] || rule.status}</strong> · v{rule.version} · {rule.descricao}{rule.inerte ? ' (inativa até existir a função de mover)' : ''}</span>
    {!replaced && !editing && <>
      <button onClick={() => send(toggleMessage(rule.id, rule.status !== 'ativa'))}>{rule.status === 'ativa' ? 'Desativar' : 'Ativar'}</button>
      <button onClick={() => setEditing(true)}>Editar</button></>}
    {editing && <span className="rule-edit">
      {rule.condicao.valor !== null && <label>Texto da condição<input value={value} maxLength={80} onChange={e => setValue(e.target.value)}/></label>}
      <label>Exceções (separe por ;)<input value={extra} onChange={e => setExtra(e.target.value)}/></label>
      <label>Ação<select value={action} onChange={e => setAction(e.target.value)}>{ACTIONS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <button onClick={save}>Salvar</button><button onClick={() => setEditing(false)}>Cancelar</button></span>}
  </li>;
}

/** The person's rules: see, turn on or off, edit. Stored only on this computer; the scope and the account cannot be edited. */
export function RulesPanel({rules, send}) {
  return <details className="persistence"><summary>Regras da caixa</summary><div className="device-options">
    <small>Regras de triagem que você aprovou. Cada uma vale só para a caixa em que foi criada, para o endereço ou o domínio exato indicado. Ficam só neste computador.</small>
    {rules.rules.length === 0 && <small>Nenhuma regra ainda. Peça, por exemplo: “Sempre trate os emails de ana@empresa.com como prioridade”.</small>}
    <ul className="memory-list">{rules.rules.map(rule => <RuleRow key={`${rule.id}-${rule.version}-${rule.status}`} rule={rule} send={send}/>)}</ul>
    {rules.message && <small role="status">{rules.message}</small>}
  </div></details>;
}
