import {useEffect, useRef, useState} from 'react';
import {Microphone, SpeechGate, wavBase64, microphoneError} from './voice.js';

export function useVoice({sendAudio, canSend, onError}) {
  const [enabled, setEnabled] = useState(false), [starting, setStarting] = useState(false);
  const [level, setLevel] = useState(0), [speaking, setSpeaking] = useState(false);
  const [devices, setDevices] = useState([]), [inputId, setInputId] = useState(''), [outputId, setOutputId] = useState('');
  const [micName, setMicName] = useState(''), [audioUrl, setAudioUrl] = useState('');
  const audio = useRef(null), microphone = useRef(null), gate = useRef(null), url = useRef(''), playbackGeneration = useRef(0);
  const latest = useRef({sendAudio, canSend, onError}); latest.current = {sendAudio, canSend, onError};
  const enabledRef = useRef(false);
  const activation = useRef(0);
  const stopPlayback = () => {playbackGeneration.current++; audio.current?.pause(); if (audio.current) audio.current.currentTime = 0; setSpeaking(false);};
  const stopMicrophone = () => {activation.current++; enabledRef.current = false; microphone.current?.stop(); gate.current?.reset(); setEnabled(false); setStarting(false); setLevel(0);};
  const stopAll = () => {stopMicrophone(); stopPlayback(); if (url.current) URL.revokeObjectURL(url.current); url.current = ''; setAudioUrl('');};
  useEffect(() => {
    microphone.current = new Microphone();
    const refresh = () => navigator.mediaDevices?.enumerateDevices().then(setDevices).catch(() => {});
    refresh(); navigator.mediaDevices?.addEventListener('devicechange', refresh);
    return () => {microphone.current?.stop(); audio.current?.pause(); if (url.current) URL.revokeObjectURL(url.current); navigator.mediaDevices?.removeEventListener('devicechange', refresh);};
  }, []);

  const startMicrophone = async (selected = inputId) => {
    const attempt = ++activation.current;
    setStarting(true); enabledRef.current = true;
    gate.current = new SpeechGate(chunks => {
      if (enabledRef.current && latest.current.canSend) latest.current.sendAudio(wavBase64(chunks));
    }, stopPlayback);
    try {
      const result = await microphone.current.start(selected, frame => {
        if (!enabledRef.current) return;
        if (latest.current.canSend) setLevel(Math.min(1, gate.current.push(frame) * 8));
        else {gate.current.reset(); setLevel(Math.min(1, Math.sqrt(frame.reduce((s,n) => s+n*n,0)/frame.length)*8));}
      }, () => {stopMicrophone(); latest.current.onError('O microfone foi desconectado. Escolha um dispositivo e ative novamente.');});
      if (result && enabledRef.current && attempt === activation.current) {setDevices(result.devices); setMicName(result.name); setInputId(result.id || selected); setEnabled(true);}
    } catch (error) {if (attempt === activation.current) {stopMicrophone(); latest.current.onError(microphoneError(error));}}
    finally {if (attempt === activation.current) setStarting(false);}
  };
  const chooseInput = async value => {setInputId(value); if (enabledRef.current) {stopMicrophone(); await startMicrophone(value);}};
  const chooseOutput = async value => {
    try {if (audio.current?.setSinkId) await audio.current.setSinkId(value); setOutputId(value);}
    catch {latest.current.onError('Não foi possível selecionar essa saída. Autorize o dispositivo no navegador ou use a saída padrão.');}
  };
  const play = async encoded => {
    stopPlayback(); const generation = playbackGeneration.current;
    if (url.current) URL.revokeObjectURL(url.current);
    const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
    url.current = URL.createObjectURL(new Blob([bytes], {type: 'audio/wav'})); setAudioUrl(url.current);
    audio.current.src = url.current;
    try {
      if (audio.current.setSinkId) await audio.current.setSinkId(outputId);
      if (generation === playbackGeneration.current) await audio.current.play();
    } catch {latest.current.onError('Áudio pronto. Clique no player para reproduzir ou confira a saída selecionada.');}
  };
  return {enabled, starting, level, speaking, devices, inputId, outputId, micName, audioUrl, audio, play, setSpeaking,
    toggle: () => enabledRef.current ? stopMicrophone() : startMicrophone(), chooseInput, chooseOutput, stopAll, stopMicrophone, stopPlayback};
}
