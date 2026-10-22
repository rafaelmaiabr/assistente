import React, {useEffect, useRef, useState} from 'react';

async function api(operation = '', body) {
  const response = await fetch(`/api/voices/${operation || 'status'}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body ?? {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Não foi possível acessar as configurações de voz.');
  return data;
}

export function VoiceSettings({onPlay, onOpen}) {
  const generation = useRef(0);
  const dialog = useRef(null);
  const [open, setOpen] = useState(false), [data, setData] = useState(null);
  const [selected, setSelected] = useState(''), [key, setKey] = useState('');
  const [busy, setBusy] = useState(''), [message, setMessage] = useState('');
  const [dirty, setDirty] = useState(false), [cloud, setCloud] = useState(false);
  const accept = result => {setData(result); setDirty(false); setSelected(id => result.settings.profiles.some(p => p.id === id) ? id : result.settings.active);};
  const run = async (label, action) => {
    setBusy(label); setMessage('');
    try {await action();} catch (error) {setMessage(error.message);}
    finally {setBusy('');}
  };
  useEffect(() => {if (open) {dialog.current?.showModal(); setCloud(false); void run('Carregando…', async () => accept(await api()));}}, [open]);
  const close = () => {generation.current++; setOpen(false); setKey('');};
  const profile = data?.settings.profiles.find(p => p.id === selected);
  const edit = changes => {
    setData(previous => ({...previous, settings: {...previous.settings,
      profiles: previous.settings.profiles.map(p => p.id === selected ? {...p, ...changes} : p)}}));
    setDirty(true); setCloud(false);
  };
  const save = async () => {accept(await api('settings', data.settings)); setMessage('Configurações salvas. Valem para a próxima resposta.');};
  const add = () => {
    const p = {...profile, id: 'voice_' + crypto.randomUUID().replaceAll('-', ''), name: profile.name + ' · cópia'};
    setData(previous => ({...previous, settings: {...previous.settings, profiles: [...previous.settings.profiles, p]}}));
    setSelected(p.id); setDirty(true); setCloud(false);
  };
  return <>
    <button className="voice-settings-button" onClick={() => {onOpen(); setOpen(true);}}>Configurar voz</button>
    {open && <dialog ref={dialog} className="voice-settings" aria-label="Configurações de voz" onCancel={close}>
      <div className="voice-settings-title"><h2>Voz do assistente</h2><button autoFocus onClick={close} aria-label="Fechar configurações de voz">Fechar</button></div>
      <p>Escolha uma voz e ajuste seu perfil. Abrir este painel desliga o microfone.</p>
      {data && profile && <fieldset disabled={!!busy}>
        <label>Perfil<select value={selected} onChange={e => {setSelected(e.target.value); setCloud(false);}}>{data.settings.profiles.map(p => <option key={p.id} value={p.id}>{p.name}{p.id === data.settings.active ? ' · em uso' : ''}</option>)}</select></label>
        <div className="voice-settings-row"><label>Nome<input maxLength={80} value={profile.name} onChange={e => edit({name: e.target.value})}/></label><button onClick={add} disabled={data.settings.profiles.length >= 20}>Duplicar perfil</button></div>
        <p><strong>{profile.provider === 'kokoro' ? 'Kokoro · local e gratuito' : profile.provider === 'windows' ? 'Windows · local e gratuito' : 'ElevenLabs · serviço externo'}</strong></p>
        {profile.provider === 'kokoro' && <>
          <label>Voz brasileira<select value={profile.voice} onChange={e => edit({voice: e.target.value})}><option value="pf_dora">Dora</option><option value="pm_alex">Alex</option><option value="pm_santa">Santa</option></select></label>
          <p>{data.kokoro_ready ? 'Modelo instalado. Síntese offline em CPU.' : 'Preparação única: download de aproximadamente 354 MB. Depois funciona offline.'}</p>
          <button onClick={() => run('Preparando Kokoro… o download pode levar alguns minutos.', async () => {accept(await api('prepare', {})); setMessage('Kokoro pronto para ouvir.');})}>Preparar / verificar Kokoro</button>
        </>}
        {profile.provider === 'elevenlabs' && <>
          <p>Quando este perfil estiver em uso, o texto das respostas será enviado ao ElevenLabs e poderá consumir créditos.</p>
          <label>Voice ID<input value={profile.voice} onChange={e => edit({voice: e.target.value})} placeholder="Copie da sua biblioteca ElevenLabs" maxLength={200}/></label>
          <label>Modelo<select value={profile.model} onChange={e => edit({model: e.target.value})}><option value="eleven_multilingual_v2">Multilingual v2</option><option value="eleven_flash_v2_5">Flash v2.5</option><option value="eleven_turbo_v2_5">Turbo v2.5</option></select></label>
          <label>Estabilidade ({profile.stability})<input type="range" min="0" max="1" step="0.05" value={profile.stability} onChange={e => edit({stability: +e.target.value})}/></label>
          <label>Similaridade ({profile.similarity})<input type="range" min="0" max="1" step="0.05" value={profile.similarity} onChange={e => edit({similarity: +e.target.value})}/></label>
          <label>Chave de API · {data.key_configured ? 'salva no cofre do Windows' : 'não configurada'}<input type="password" autoComplete="off" spellCheck="false" value={key} onChange={e => setKey(e.target.value)} placeholder="Cole sua chave aqui; ela não será exibida novamente" maxLength={1000}/></label>
          <div className="voice-settings-row"><button disabled={!key} onClick={() => run('Salvando chave…', async () => {const result = await api('credential', {key}); setKey(''); setData(previous => ({...previous, key_configured: result.key_configured})); setMessage('Chave salva no Windows.');})}>Salvar chave</button><button disabled={!data.key_configured} onClick={() => run('Removendo chave…', async () => {const result = await api('credential', {key: null}); setData(previous => ({...previous, key_configured: result.key_configured})); setKey(''); setMessage('Chave removida.');})}>Remover chave</button></div>
          <small>Compartilhada entre os perfis ElevenLabs desta instalação. Não vai para o JSON nem para o Git.</small>
          <label className="voice-consent"><input type="checkbox" checked={cloud} onChange={e => setCloud(e.target.checked)}/> Autorizar prévia externa com possível consumo de créditos</label>
        </>}
        <label>Velocidade ({profile.speed.toFixed(2)}×)<input type="range" min={profile.provider === 'elevenlabs' ? '0.7' : '0.5'} max={profile.provider === 'elevenlabs' ? '1.2' : '2'} step="0.05" value={profile.speed} onChange={e => edit({speed: +e.target.value})}/></label>
        <label className="voice-consent"><input type="checkbox" checked={data.settings.active === selected} onChange={e => {if (e.target.checked) {setData(previous => ({...previous, settings: {...previous.settings, active: selected}})); setDirty(true);}}}/> Usar este perfil nas respostas {profile.provider === 'elevenlabs' ? '(envia texto ao ElevenLabs)' : ''}</label>
        <div className="voice-settings-row"><button onClick={() => run('Salvando…', save)}>Salvar configurações</button><button disabled={dirty || (profile.provider === 'kokoro' && !data.kokoro_ready) || (profile.provider === 'elevenlabs' && (!cloud || !profile.voice || !data.key_configured))} onClick={() => {const attempt = generation.current; void run('Gerando prévia…', async () => {const result = await api('preview', {profile: selected, allow_cloud: cloud}); if (attempt === generation.current) {await onPlay(result.wav); setMessage('Prévia gerada.');}});}}>Ouvir prévia{profile.provider === 'elevenlabs' ? ' (API)' : ''}</button></div>
        {dirty && <small>Salve as alterações antes de ouvir a prévia ou fechar.</small>}
      </fieldset>}
      <p role="status">{busy || message}</p>
    </dialog>}
  </>;
}
