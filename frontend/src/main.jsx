import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
import {useVoice} from './useVoice.js';
import {VoiceSettings} from './VoiceSettings.jsx';
import {PersistenceSettings} from './PersistenceSettings.jsx';
import {TeamPanel} from './team/TeamPanel.jsx';

function Icon({name, ...props}) {
  const paths = {
    plus: 'M12 5v14M5 12h14', arrow: 'M12 19V5m-6 6 6-6 6 6',
    chat: 'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9H13a8.5 8.5 0 0 1 8 8v.5Z',
    mail: 'M4 5h16v14H4zM4 6l8 6 8-6', shield: 'M12 3l8 4v5c0 5-8 9-8 9s-8-4-8-9V7l8-4Z',
    spark: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
    menu: 'M4 6h16M4 12h16M4 18h16', close: 'm6 6 12 12M18 6 6 18',
    mic: 'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Zm-3 6v1a6 6 0 0 0 12 0v-1M12 18v4m-3 0h6',
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}><path d={paths[name] || paths.spark}/></svg>;
}

function App() {
  const [epoch, setEpoch] = useState(0);
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState('');
  const [state, setState] = useState('connecting');
  const [label, setLabel] = useState('Conectando…');
  const [model, setModel] = useState('Modelo local');
  const [error, setError] = useState('');
  const [fatal, setFatal] = useState(false);
  const [tools, setTools] = useState([]);
  const [menu, setMenu] = useState(false);
  const [view, setView] = useState('chat');
  const [speak, setSpeak] = useState(true);
  const speakRef = useRef(true);
  const voiceRef = useRef(null);
  const socket = useRef(null);
  const busy = useRef(true);
  const bottom = useRef(null);
  const input = useRef(null);
  const voice = useVoice({canSend: state === 'ready' && !fatal, onError: setError, sendAudio: wav => {
    if (busy.current || socket.current?.readyState !== WebSocket.OPEN) return;
    busy.current = true; setState('transcribing'); setLabel('Transcrevendo sua fala…'); setError(''); setTools([]);
    socket.current.send(JSON.stringify({type: 'audio', wav, speak: speakRef.current}));
  }});
  voiceRef.current = voice;
  const desktopStatus = useRef(null);
  desktopStatus.current = {state: state === 'ready' ? (error ? 'error' : voice.speaking ? 'speaking' : voice.enabled ? 'listening' : 'ready') : state,
    micEnabled: voice.enabled, level: voice.level};
  useEffect(() => {
    if (!window.desktopBridge) return;
    const publish = () => window.desktopBridge.publishStatus(desktopStatus.current);
    publish(); const interval = setInterval(publish, 150);
    const unsubscribe = window.desktopBridge.onCommand(command => {
      if (command === 'toggle-mic') {setError(''); voiceRef.current.toggle();}
    });
    return () => {clearInterval(interval); unsubscribe();};
  }, []);

  useEffect(() => {
    let active = true;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket.current = ws;
    busy.current = true;
    setState('connecting'); setLabel('Preparando assistente…'); setFatal(false); setError('');
    ws.onmessage = event => {
      if (!active) return;
      const data = JSON.parse(event.data);
      if (data.type === 'session') setModel(data.model);
      if (data.type === 'status') {
        setState(data.state); setLabel(data.label);
        busy.current = data.state !== 'ready';
      }
      if (data.type === 'message') setMessages(items => [...items, data]);
      if (data.type === 'audio' && speakRef.current) void voiceRef.current.play(data.wav);
      if (data.type === 'warning') setError(data.message);
      if (data.type === 'tool') setTools(items => [...items, data]);
      if (data.type === 'error') {
        setError(data.message); setFatal(data.fatal); setState(data.fatal ? 'offline' : 'ready');
        setLabel(data.fatal ? 'Conversa interrompida' : 'Falha na resposta'); busy.current = data.fatal;
        if (data.fatal) voiceRef.current.stopAll();
      }
      if (data.type === 'rejected') {
        setError(data.message);
        if (!data.busy) {busy.current = false; setState('ready'); setLabel('Pronto para conversar');}
      }
    };
    ws.onclose = () => {
      if (!active) return;
      busy.current = true; setState('offline'); setLabel('Desconectado'); setFatal(true);
      voiceRef.current.stopAll();
      setError(previous => previous || 'A conexão local foi encerrada. Inicie uma nova conversa para reconectar.');
    };
    ws.onerror = () => {
      if (active) setError('Não foi possível conectar ao assistente local. Verifique se o servidor está aberto.');
    };
    return () => {active = false; voiceRef.current.stopAll(); ws.close();};
  }, [epoch]);

  useEffect(() => {
    if (messages.length) bottom.current?.scrollIntoView({behavior: 'smooth'});
    else bottom.current?.parentElement?.scrollTo({top: 0});
  }, [messages, state, tools]);
  const waiting = ['connecting', 'processing', 'executing', 'transcribing', 'synthesizing'].includes(state);
  const canSend = state === 'ready' && !fatal;
  const send = (value = text) => {
    const content = value.trim();
    if (!content || content.length > 8000 || busy.current || socket.current?.readyState !== WebSocket.OPEN || fatal) return;
    busy.current = true; setState('processing'); setLabel('Enviando…'); setError(''); setTools([]);
    voice.stopPlayback();
    socket.current.send(JSON.stringify({type: 'message', text: content, speak: speakRef.current}));
    setText(''); input.current?.focus();
  };
  const reset = () => {setMessages([]); setText(''); setTools([]); setMenu(false); setView('chat'); setEpoch(n => n + 1);};
  // Leaving the conversation always turns the microphone off: capture never continues out of sight.
  const go = next => {if (next !== 'chat') voiceRef.current.stopAll(); setView(next); setMenu(false);};

  return <div className="app">
    {menu && <button className="scrim" aria-label="Fechar menu" onClick={() => setMenu(false)}/>}
    <aside className={menu ? 'sidebar open' : 'sidebar'}>
      <a className="brand" href="#" onClick={e => e.preventDefault()}><span className="brand-mark"><Icon name="spark"/></span> assistente<span className="beta">LOCAL</span></a>
      <button className="new-chat" onClick={reset}><Icon name="plus"/> Nova conversa</button>
      <div className="nav-label">SEU ESPAÇO</div>
      <button className={view === 'chat' ? 'nav-item active' : 'nav-item'} aria-current={view === 'chat' ? 'page' : undefined} onClick={() => go('chat')}><Icon name="chat"/><span>Conversa atual</span>{view === 'chat' && <span className="nav-dot"/>}</button>
      <button className={view === 'team' ? 'nav-item active' : 'nav-item'} aria-current={view === 'team' ? 'page' : undefined} onClick={() => go('team')}><Icon name="shield"/><span>Equipe</span></button>
      <div className="sidebar-note">Um espaço para pensar,<br/>perguntar e fazer.</div>
      <div className="sidebar-bottom">
        <div className="privacy"><Icon name="shield"/><div><strong>Feito para rodar localmente</strong><p>Histórico apenas nesta sessão.</p></div></div>
        <div className="profile"><span className="profile-icon">EU</span><div><strong>Meu espaço</strong><small>Validação do assistente</small></div><span className="online-dot"/></div>
      </div>
    </aside>
    {view === 'team' ? <TeamPanel onOpenMenu={() => setMenu(true)}/> : <main>
      <header>
        <div className="header-left"><button className="menu-button" aria-label="Abrir menu" onClick={() => setMenu(true)}><Icon name="menu"/></button><span className="page-title">Conversa</span><span className="separator">/</span><span className="model">{model}</span></div>
        <div className={`connection ${state === 'offline' ? 'off' : ''}`}><span/> {state === 'offline' ? 'Desconectado' : state === 'connecting' ? 'Conectando' : 'Sessão local'}</div>
      </header>
      <div className="scroll-area">
        {messages.length === 0 ? <section className="welcome">
          <div className="hero-mark"><Icon name="spark" width="36" height="36"/></div>
          <div className="eyebrow">SEU ASSISTENTE, NO SEU COMPUTADOR</div>
          <h1>O que vamos fazer hoje?</h1>
          <p>Uma pergunta, uma ideia ou uma tarefa.<br/>É só começar a conversa.</p>
          <div className="suggestions">
            <button disabled={!canSend} onClick={() => send('Verifique a caixa de email X e resuma as mensagens.')}><Icon name="mail"/><strong>Consultar meus e-mails</strong><span>Experimentar com uma caixa fictícia</span><span className="card-arrow">↗</span></button>
            <button disabled={!canSend} onClick={() => send('Me ajude a organizar minhas prioridades para hoje. Faça uma pergunta de cada vez.')}><Icon name="spark"/><strong>Organizar minhas ideias</strong><span>Encontrar um bom ponto de partida</span><span className="card-arrow">↗</span></button>
          </div>
          <div className="demo-note">A consulta de e-mails usa dados de demonstração. Nenhuma conta real está conectada.</div>
        </section> : <section className="messages" aria-label="Histórico da conversa" aria-live="polite" aria-relevant="additions">
          {messages.map(item => <article key={item.id} className={`message ${item.role}`}><div className="message-avatar">{item.role === 'assistant' ? <Icon name="spark"/> : 'EU'}</div><div className="message-content"><div className="message-author">{item.role === 'assistant' ? 'Assistente' : 'Você'}</div><div className="message-text">{item.text}</div></div></article>)}
          {waiting && <div className="working"><span className="spinner"/>{label}</div>}
          {tools.length > 0 && <div className="tool-result"><Icon name="mail"/>{tools.some(t => t.state === 'error') ? 'A ferramenta encontrou um erro.' : tools.some(t => t.state === 'done') ? 'Consulta de ferramenta concluída nesta resposta.' : 'Ferramenta em execução…'}</div>}
        </section>}
        <div ref={bottom}/>
      </div>
      <div className="composer-area">
        {error && <div className="error" role="alert"><span>{error}</span>{fatal && <button onClick={reset}>Nova conversa</button>}</div>}
        <div className="voice-controls">
          <VoiceSettings onPlay={wav => voice.play(wav)} onOpen={() => voice.stopAll()}/>
          <button className={`mic-button ${voice.enabled ? 'listening' : ''}`} disabled={state === 'offline' || state === 'connecting'} aria-pressed={voice.enabled} onClick={() => {setError(''); voice.toggle();}}><Icon name="mic"/>{voice.starting ? 'Cancelar ativação' : voice.enabled ? 'Silenciar microfone' : 'Ligar microfone'}</button>
          <label>Resposta<select aria-label="Formato da resposta" value={speak ? 'audio' : 'text'} onChange={e => {const value = e.target.value === 'audio'; setSpeak(value); speakRef.current = value; if (!value) voice.stopPlayback();}}><option value="audio">Texto e áudio</option><option value="text">Somente texto</option></select></label>
          <PersistenceSettings/>
          <details><summary>Dispositivos de áudio</summary><div className="device-options">
            <label>Microfone<select aria-label="Microfone" value={voice.inputId} onChange={e => voice.chooseInput(e.target.value)}><option value="">Automático (preferir físico)</option>{voice.devices.filter(d => d.kind === 'audioinput').map((d,i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Microfone ${i+1}`}</option>)}</select></label>
            <label>Saída<select aria-label="Saída de áudio" value={voice.outputId} disabled={!('setSinkId' in HTMLMediaElement.prototype)} onChange={e => voice.chooseOutput(e.target.value)}><option value="">Padrão do sistema</option>{voice.devices.filter(d => d.kind === 'audiooutput' && d.deviceId !== 'default').map((d,i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Saída ${i+1}`}</option>)}</select></label>
            <small>Os nomes aparecem após autorizar o microfone. Use fones para evitar retorno de áudio. A troca de saída depende do navegador.</small>
          </div></details>
        </div>
        <div className="status-line" role="status"><span className={`status-dot ${waiting || voice.speaking ? 'pulse' : ''} ${state === 'offline' ? 'off' : ''}`}/>{voice.starting ? 'Autorize o microfone no navegador…' : voice.speaking ? 'Respondendo em áudio…' : voice.enabled && canSend ? 'Ouvindo · fale e faça uma pausa' : label}<span className="text-mode">{voice.enabled ? 'Captura ativa' : 'Microfone desligado'}</span></div>
        {voice.enabled && <div className="mic-level"><meter aria-label="Nível do microfone" min="0" max="1" value={voice.level}/><span>{voice.micName}{waiting ? ' · aguarde a resposta para continuar' : ''}</span></div>}
        <div className="audio-response" hidden={!voice.audioUrl}><audio ref={voice.audio} controls onPlaying={() => voice.setSpeaking(true)} onPause={() => voice.setSpeaking(false)} onEnded={() => voice.setSpeaking(false)} aria-label="Áudio da última resposta"/><button onClick={voice.stopPlayback}>Parar áudio</button></div>
        <form className="composer" onSubmit={e => {e.preventDefault(); send();}}>
          <textarea ref={input} aria-label="Mensagem" placeholder="Pergunte ou peça alguma coisa…" value={text} maxLength={8000} rows={2} onChange={e => setText(e.target.value)} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); send();}}}/>
          <div className="composer-bottom"><span>Enter para enviar <span className="key-hint">· Shift + Enter para nova linha</span></span><button className="send" aria-label="Enviar mensagem" disabled={!canSend || !text.trim()} type="submit"><Icon name="arrow"/></button></div>
        </form>
        <p className="footer-note">A IA pode cometer erros. Ao iniciar uma nova conversa ou atualizar a página, o histórico é descartado.</p>
      </div>
    </main>}
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
