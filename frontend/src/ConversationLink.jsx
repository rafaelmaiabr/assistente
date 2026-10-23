import React, {useEffect, useState} from 'react';
import {api} from './team/api.js';
import {describeError} from './team/model.js';
import {bindingLabel, controls} from './conversationModel.js';

/** Who this conversation is attached to, the four separate controls, and the way to open the right case. */
export function ConversationLink({conv, view, onAttach, onDetach, onInterrupt, onStopSpeech, onOpenCase, onPause}) {
  const [open, setOpen] = useState(false), [companies, setCompanies] = useState([]), [cases, setCases] = useState([]);
  const [company, setCompany] = useState(''), [caseId, setCaseId] = useState(''), [message, setMessage] = useState('');
  const can = controls(conv, view), binding = conv.binding, idle = !conv.running;
  useEffect(() => {
    if (!open) return;
    let alive = true;
    api.get('/state').then(data => {if (alive) {setCompanies(data.enabled ? data.companies : []); if (!data.enabled) setMessage('O atendimento persistente está desligado: ative-o na seção Equipe.');}})
      .catch(error => alive && setMessage(describeError(error)));
    return () => {alive = false;};
  }, [open]);
  useEffect(() => {
    setCases([]); setCaseId('');
    if (!open || !company) return;
    let alive = true;
    api.get(`/companies/${company}/cases`).then(data => alive && setCases(data.cases.filter(c => !['concluida', 'cancelada'].includes(c.phase))))
      .catch(error => alive && setMessage(describeError(error)));
    return () => {alive = false;};
  }, [open, company]);
  const pause = async () => {
    setMessage('');
    try {await onPause(binding); setMessage('Demanda pausada. A chamada continua; o microfone não foi alterado.');}
    catch (error) {setMessage(describeError(error));}
  };
  return <section className="conversation-link" aria-label="Vínculo da conversa">
    <div className="binding-line">
      <span className={`binding-chip ${binding ? 'team' : ''}`} title="Com quem esta conversa está falando agora">{bindingLabel(binding)}</span>
      {binding?.waiting_human && <span className="badge human">Aguardando uma pessoa</span>}
      {binding?.paused && <span className="badge">Pausada</span>}
      {binding?.operator_control && <span className="badge human">Sob controle do operador</span>}
      {conv.latencyMs != null && <small className="latency" title="Tempo até a primeira resposta">{conv.latencyMs} ms</small>}
      <div className="binding-actions">
        <button type="button" disabled={!can.stopSpeech} onClick={onStopSpeech} title="Para só o áudio que está tocando">Parar fala</button>
        <button type="button" disabled={!can.interrupt} onClick={onInterrupt} title="Cancela a resposta que está sendo gerada">Interromper resposta</button>
        <button type="button" disabled={!can.pauseCase} onClick={pause} title="Pausa a demanda, não a conversa">Pausar demanda</button>
        {binding?.case && <button type="button" onClick={() => onOpenCase({company_id: binding.company.id, case_id: binding.case.id})}>Abrir demanda ↗</button>}
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{binding ? 'Trocar vínculo' : 'Vincular a uma demanda'}</button>
      </div>
    </div>
    {open && <form className="binding-picker" onSubmit={event => {event.preventDefault(); if (company && caseId) {onAttach({company_id: company, case_id: caseId}); setOpen(false);}}}>
      <label>Empresa<select value={company} onChange={event => setCompany(event.target.value)}><option value="">Escolha…</option>{companies.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
      <label>Demanda<select value={caseId} disabled={!company} onChange={event => setCaseId(event.target.value)}><option value="">Escolha…</option>{cases.map(c => <option key={c.id} value={c.id}>{c.objective.slice(0, 60)}</option>)}</select></label>
      <button type="submit" disabled={!idle || !company || !caseId}>Vincular</button>
      {binding && <button type="button" disabled={!idle} onClick={() => {onDetach(); setOpen(false);}}>Desvincular</button>}
      {!idle && <small>Aguarde o fim da resposta para trocar o vínculo.</small>}
    </form>}
    {message && <p className="hint" role="status">{message}</p>}
    <small className="hint">O vínculo mostra a empresa, a demanda e o agente responsável; a voz acompanha o responsável depois do aceite de uma transferência. Ligar ou desligar o microfone é sempre um clique seu.</small>
  </section>;
}
