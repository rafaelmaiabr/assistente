import React, {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';
import {PHASE_CLASS, PHASE_LABEL, STATE_LABEL, approvalLines, canDecide, canRevoke, describeEvent, mergeEvents, needsRefresh, nextDelay,
  GAP_DECISIONS, GAP_TYPE_LABEL, canDecideGap, canFulfillGap, gapDecisionBody, gapState, shortHash} from './casesModel.js';
import {Notice} from './shared.jsx';

const PHASES = ['', 'aguardando_humano', 'andamento', 'aguardando_cliente', 'falha', 'concluida', 'cancelada'];

/** Keeps the list and the open case current: events after a cursor say *that* something changed; the answer is always a fresh snapshot. */
function useLive(base, openId) {
  const [list, setList] = useState({cases: [], cursor: 0}), [snap, setSnap] = useState(null);
  const [online, setOnline] = useState(true), [error, setError] = useState('');
  const cursor = useRef(0), failures = useRef(0), known = useRef([]), open = useRef(openId);
  open.current = openId;
  const load = useCallback(async () => {
    const [cases, detail] = await Promise.all([api.get(`${base}/cases`), open.current ? api.get(`${base}/cases/${open.current}`) : Promise.resolve(null)]);
    cursor.current = Math.max(cases.cursor, detail?.cursor || 0);
    setList(cases); setSnap(detail); setError('');
  }, [base]);
  const reload = useCallback(async () => {try {await load();} catch (e) {setError(describeError(e));}}, [load]);
  useEffect(() => {cursor.current = 0; known.current = []; void reload();}, [base, openId, reload]);
  useEffect(() => {
    let stop = false, timer;
    const tick = async () => {
      if (stop) return;
      if (!document.hidden) {
        try {
          const feed = await api.get(`${base}/events?after=${cursor.current}`);
          const merged = mergeEvents(known.current, feed.events, cursor.current);
          const fresh = merged.events.length - known.current.length;
          known.current = merged.events.slice(-500); cursor.current = merged.cursor;
          if (failures.current > 0) {failures.current = 0; setOnline(true); await load();}  // back online: re-read everything
          else if (fresh > 0 && needsRefresh(feed.events)) await load();
        } catch {failures.current += 1; setOnline(false);}
      }
      timer = setTimeout(tick, nextDelay(failures.current));
    };
    timer = setTimeout(tick, nextDelay(0));
    return () => {stop = true; clearTimeout(timer);};
  }, [base, load]);
  return {list, snap, online, error, reload};
}

function Badge({phase}) {return <span className={`badge phase-${PHASE_CLASS[phase]}`}>{PHASE_LABEL[phase]}</span>;}

function Approval({base, approval, onChanged}) {
  const [confirmed, setConfirmed] = useState(false), [text, setText] = useState(''), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, body) => {
    setBusy(true); setMessage({});
    try {await api.post(`${base}/approvals/${approval.id}/${action}`, body); setConfirmed(false); setText(''); await onChanged();}
    catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}
  };
  return <article className={`team-approval state-${approval.state}`} aria-label={`Aprovação: ${approval.action}`}>
    <dl>{approvalLines(approval).map(([k, v]) => <React.Fragment key={k}><dt>{k}</dt><dd>{v}</dd></React.Fragment>)}</dl>
    <p className="hint">Identificação do conteúdo: <code>{shortHash(approval.payload_hash)}</code> · estado: <strong>{approval.state}</strong>
      {approval.effect_outcome && <> · efeito: <strong>{approval.effect_outcome}</strong></>}</p>
    {(canDecide(approval) || canRevoke(approval)) && <>
      <label>Comentário ou motivo<input value={text} maxLength={300} onChange={e => setText(e.target.value)}/></label>
      {canDecide(approval) && <label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}/> Conferi a ação, o destinatário e o valor acima.</label>}
      <div className="team-actions">
        {canDecide(approval) && <button className="primary" disabled={busy || !confirmed} onClick={() => run('approve', {shown_hash: approval.payload_hash, comment: text || undefined})}>Aprovar</button>}
        {canDecide(approval) && <button disabled={busy || !text.trim()} onClick={() => run('deny', {reason: text})}>Negar</button>}
        {canRevoke(approval) && <button disabled={busy || !text.trim()} onClick={() => run('revoke', {reason: text})}>Revogar</button>}
      </div></>}
    {approval.effect_outcome === 'desconhecido' && <div className="team-reconcile" role="alert">
      <strong>Resultado desconhecido.</strong> Confira no sistema externo antes de decidir; nada é desfeito automaticamente.
      <div className="team-actions">
        <button disabled={busy || !text.trim()} onClick={() => run('resolve-effect', {outcome: 'confirmado', detail: text})}>Confirmar que aconteceu</button>
        <button disabled={busy || !text.trim()} onClick={() => run('resolve-effect', {outcome: 'falhou', detail: text})}>Confirmar que não aconteceu</button></div></div>}
    <Notice error={message.error}/>
  </article>;
}

function Gap({base, gap, onChanged}) {
  const [note, setNote] = useState(''), [followUp, setFollowUp] = useState(''), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const run = async (action, body) => {
    setBusy(true); setMessage({});
    try {await api.post(`${base}/gaps/${gap.id}/${action}`, body); setNote(''); setFollowUp(''); await onChanged();}
    catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}
  };
  return <article className={`team-approval state-${gap.state}`} aria-label={`Lacuna: ${gap.topic}`}>
    <p><strong>{gap.requested}</strong><br/><span className="badge human">{gapState(gap)}</span> <small>{GAP_TYPE_LABEL[gap.gap_type]} · {gap.occurrences} ocorrência(s)</small></p>
    {gap.tried.length > 0 && <ul className="team-list compact">{gap.tried.map(t => <li key={t}><span>Tentado: {t}</span></li>)}</ul>}
    {gap.decision_note && <p className="hint">Justificativa: {gap.decision_note}{gap.follow_up && <> · acompanhamento: {gap.follow_up}</>}</p>}
    {(canDecideGap(gap) || canFulfillGap(gap)) && <>
      <label>Justificativa<input value={note} maxLength={300} onChange={e => setNote(e.target.value)}/></label>
      {canDecideGap(gap) && <label>Referência do trabalho (opcional)<input value={followUp} maxLength={120} onChange={e => setFollowUp(e.target.value)}/></label>}
      <div className="team-actions">
        {canDecideGap(gap) && GAP_DECISIONS.map(([value, label]) => <button key={value} disabled={busy || !note.trim()}
          onClick={() => run('decide', gapDecisionBody(gap, value, note, followUp))}>{label}</button>)}
        {canFulfillGap(gap) && <button className="primary" disabled={busy || !note.trim()} onClick={() => run('fulfill', {expected_revision: gap.revision, note: note.trim()})}>Capacidade pronta: retomar</button>}
      </div>
      {canDecideGap(gap) && <p className="hint">Não atender cancela as demandas ligadas e guarda uma memória para a triagem reconhecer o pedido; as outras duas mantêm a demanda pausada.</p>}</>}
    <Notice error={message.error}/>
  </article>;
}

function Detail({base, snap, reload}) {
  const {case: c} = snap;
  const [reason, setReason] = useState(''), [message, setMessage] = useState({}), [busy, setBusy] = useState(false);
  const act = async action => {
    setBusy(true); setMessage({});
    try {await api.post(`${base}/cases/${c.id}/${action}`, {expected_revision: c.revision, reason}); setReason(''); await reload();}
    catch (e) {setMessage({error: describeError(e)});} finally {setBusy(false);}
  };
  const closed = ['resolvida', 'cancelada', 'falhou'].includes(c.state);
  return <section className="team-case" aria-label="Detalhe da demanda">
    <h3>{c.objective}</h3>
    <p><Badge phase={c.phase}/> <span className="badge">{STATE_LABEL[c.state]}</span> {c.operator_control && <span className="badge human">sob controle do operador</span>}
      <br/>Responsável: <strong>{c.owner_name || 'fila inicial (sem responsável)'}</strong> · prioridade {c.priority || 'normal'} · origem {c.origin || '—'}</p>
    {c.reconciliation > 0 && <p className="team-reconcile" role="alert">Há {c.reconciliation} operação(ões) com resultado desconhecido que precisa(m) de reconciliação.</p>}
    {!closed && <div className="team-form" aria-label="Intervenção do operador">
      <label>Motivo (obrigatório para intervir)<input value={reason} maxLength={300} onChange={e => setReason(e.target.value)}/></label>
      <div className="team-actions">
        {!c.operator_control && <button disabled={busy || !reason.trim()} onClick={() => act('take-over')}>Assumir</button>}
        {!c.operator_control && c.state !== 'pausada' && <button disabled={busy || !reason.trim()} onClick={() => act('pause')}>Pausar</button>}
        {c.operator_control && <button className="primary" disabled={busy || !reason.trim()} onClick={() => act('return')}>Devolver à equipe</button>}
        <button disabled={busy || !reason.trim()} onClick={() => act('reject')}>Rejeitar demanda</button></div>
      <p className="hint">Cada ação usa a revisão {c.revision}; se outra pessoa mudou a demanda, você será avisado para recarregar.</p>
      <Notice error={message.error}/></div>}
    {snap.gaps?.length > 0 && <><h4>Lacunas de capacidade</h4>{snap.gaps.map(g => <Gap key={g.id} base={base} gap={g} onChanged={reload}/>)}</>}
    <h4>Aprovações</h4>
    {snap.approvals.map(a => <Approval key={a.id} base={base} approval={a} onChanged={reload}/>)}
    {!snap.approvals.length && <p className="team-empty">Nenhuma aprovação.</p>}
    <h4>Subtarefas e transferências</h4>
    <ul className="team-list compact">
      {snap.tasks.map(t => <li key={t.id}><span>Subtarefa para <strong>{t.assignee_name}</strong>: {t.objective} <span className="badge">{t.state}</span></span></li>)}
      {snap.handoffs.map(h => <li key={h.id}><span>{h.handoff_kind === 'transferencia' ? 'Transferência' : 'Delegação'}: {h.from_name} → {h.to_name} <span className={`badge ${h.attention ? 'human' : ''}`}>{h.state}</span>
        {h.resolution && <small> {h.resolution}</small>}</span></li>)}
      {!snap.tasks.length && !snap.handoffs.length && <li className="team-empty">Nenhuma subtarefa ou transferência.</li>}</ul>
    <h4>Mensagens ao cliente</h4>
    <ul className="team-list compact">{snap.messages.map(m => <li key={m.id}><span>{m.subject || '(sem assunto)'} → {m.to.join(', ')} <span className={`badge ${m.state === 'desconhecido' ? 'human' : ''}`}>{m.state}</span>
      {m.state === 'enviada' && <small> aceita pelo provedor ({m.provider_id})</small>}</span></li>)}
      {!snap.messages.length && <li className="team-empty">Nenhuma mensagem.</li>}</ul>
    <h4>Linha do tempo</h4>
    <ol className="team-timeline">{snap.timeline.map(item => <li key={item.item.id} className={`t-${item.tipo}`}>
      <small>{item.em}</small>{' '}
      {item.tipo === 'evento' ? <span>{describeEvent(item.item.type)}</span>
        : item.tipo === 'evidencia' ? <span><strong>Evidência</strong> ({item.item.reliability}): {item.item.summary} <small>fonte: {item.item.source}</small></span>
        : <span><strong>{{nota_interna: 'Nota interna', mensagem_externa: item.item.direction === 'entrada' ? 'Mensagem do cliente' : 'Mensagem ao cliente', resumo_ia: 'Resumo gerado (não é prova)'}[item.item.entry_type]}</strong>: {item.item.body}</span>}
    </li>)}</ol>
  </section>;
}

export function CasesSection({base, openRequest = ''}) {
  const [phase, setPhase] = useState(''), [openId, setOpenId] = useState(openRequest);
  useEffect(() => {if (openRequest) setOpenId(openRequest);}, [openRequest]);
  const {list, snap, online, error, reload} = useLive(base, openId);
  const cases = list.cases.filter(c => !phase || c.phase === phase);
  return <section aria-labelledby="cases-title" className="team-split">
    <div>
      <h2 id="cases-title">Demandas</h2>
      <p className={online ? 'hint' : 'team-error'} role="status" aria-live="polite">{online ? 'Atualizando automaticamente.' : 'Sem conexão. Tentando reconectar…'}</p>
      <Notice error={error}/>
      <label>Mostrar<select value={phase} onChange={e => setPhase(e.target.value)}>{PHASES.map(p => <option key={p} value={p}>{p ? PHASE_LABEL[p] : 'Todas'}</option>)}</select></label>
      <ul className="team-list">{cases.map(c => <li key={c.id} className={c.id === openId ? 'selected' : ''}>
        <button className="link" onClick={() => setOpenId(c.id)} aria-current={c.id === openId ? 'true' : undefined}>
          <strong>{c.objective}</strong><br/><Badge phase={c.phase}/> <small>{c.owner_name || 'sem responsável'}</small>
          {c.pending_approvals > 0 && <span className="badge human">{c.pending_approvals} aprovação(ões)</span>}
          {c.open_gaps > 0 && <span className="badge human">lacuna</span>}
          {c.reconciliation > 0 && <span className="badge human">reconciliar</span>}</button></li>)}
        {!cases.length && <li className="team-empty">Nenhuma demanda neste filtro.</li>}</ul>
    </div>
    <div>{snap ? <Detail base={base} snap={snap} reload={reload} key={snap.case.id}/> : <p className="team-empty">Selecione uma demanda para ver os detalhes.</p>}</div>
  </section>;
}
