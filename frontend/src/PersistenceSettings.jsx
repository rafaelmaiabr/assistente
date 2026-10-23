import React, {useEffect, useState} from 'react';

async function call(path, method = 'GET') {
  const response = await fetch(`/api/persistence${path}`, {method, headers: {'Content-Type': 'application/json'}, body: method === 'POST' ? '{}' : undefined});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Não foi possível acessar o atendimento persistente.');
  return data;
}

export function PersistenceSettings() {
  const [data, setData] = useState(null), [busy, setBusy] = useState(false), [problem, setProblem] = useState('');
  const apply = result => {setData(result); setProblem(result.error || '');};
  useEffect(() => {call('').then(apply).catch(error => setProblem(error.message));}, []);
  const toggle = async () => {
    setBusy(true); setProblem('');
    try {apply(await call(data.enabled ? '/disable' : '/enable', 'POST'));}
    catch (error) {setProblem(error.message);}
    finally {setBusy(false);}
  };
  return <details className="persistence"><summary>Atendimento persistente</summary><div className="device-options">
    <small role="status">{data ? data.notice : 'Carregando…'}</small>
    {problem && <small role="alert">{problem}</small>}
    <button type="button" disabled={!data || busy} onClick={toggle}>{busy ? 'Aguarde…' : data?.enabled ? 'Desativar' : 'Ativar'}</button>
  </div></details>;
}
