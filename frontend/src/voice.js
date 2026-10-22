export function wavBase64(chunks) {
  const count = chunks.reduce((total, part) => total + part.length, 0);
  const buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
  const word = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  word(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); word(8, 'WAVE'); word(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  word(36, 'data'); view.setUint32(40, count * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const value of chunk) {
    view.setInt16(offset, Math.max(-1, Math.min(1, value)) * 32767, true); offset += 2;
  }
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

export class SpeechGate {
  constructor(onSpeech, onStart) {this.onSpeech = onSpeech; this.onStart = onStart; this.reset();}
  reset() {this.chunks = []; this.pre = []; this.speech = 0; this.silence = 0; this.total = 0; this.started = false;}
  push(frame) {
    const rms = Math.sqrt(frame.reduce((sum, n) => sum + n * n, 0) / frame.length);
    const loud = rms > 0.012;
    if (!this.chunks.length && !loud) {
      this.pre.push(frame); if (this.pre.length > 15) this.pre.shift();
      return rms;
    }
    if (!this.chunks.length) {this.chunks = this.pre; this.pre = []; this.total = this.chunks.length * 320;}
    this.chunks.push(frame); this.total += frame.length;
    if (loud) {this.speech += frame.length; this.silence = 0;} else this.silence += frame.length;
    if (!this.started && this.speech >= 3200) {this.started = true; this.onStart();}
    if (this.silence >= 14400 || this.total >= 320000) {
      const chunks = this.chunks, valid = this.speech >= 4800;
      this.reset();
      if (valid) this.onSpeech(chunks);
    }
    return rms;
  }
}

export function microphoneError(error) {
  if (['NotAllowedError', 'SecurityError'].includes(error.name)) return 'Permissão de microfone negada. Libere o microfone nas permissões deste site e tente novamente.';
  if (['NotFoundError', 'OverconstrainedError'].includes(error.name)) return 'Microfone não encontrado. Conecte um dispositivo ou escolha outro na lista.';
  if (error.name === 'NotReadableError') return 'Não foi possível abrir o microfone. Confira se outro aplicativo está usando o dispositivo.';
  return error.message || 'Não foi possível iniciar o microfone.';
}

export class Microphone {
  constructor(media = navigator.mediaDevices, Context = window.AudioContext, permissionTimeout = 20000) {
    this.media = media; this.Context = Context; this.generation = 0; this.permissionTimeout = permissionTimeout;
  }
  async acquire(constraints) {
    let expired = false, timer;
    const request = this.media.getUserMedia(constraints).then(stream => {
      if (expired) stream.getTracks().forEach(track => track.stop());
      return stream;
    });
    try {
      return await Promise.race([request, new Promise((_, reject) => {
        timer = setTimeout(() => {expired = true; reject(new Error('O navegador não liberou o microfone. Autorize a captura ou tente no Chrome, Edge ou aplicativo desktop.'));}, this.permissionTimeout);
      })]);
    } finally {clearTimeout(timer);}
  }
  async start(device, onFrame, onEnded) {
    this.stop(); const generation = this.generation;
    if (!this.media?.getUserMedia || !this.Context) throw new Error('Use Chrome ou Edge atualizado em http://127.0.0.1:8765.');
    let stream, context;
    try {
      const constraints = id => ({audio: {deviceId: id ? {exact: id} : undefined, echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false});
      stream = await this.acquire(constraints(device));
      if (generation !== this.generation) {stream.getTracks().forEach(t => t.stop()); return null;}
      const devices = await this.media.enumerateDevices();
      // Avoid a silent virtual Broadcast/Steam device when a physical mic exists.
      const physical = devices.find(d => d.kind === 'audioinput' && !['default', 'communications'].includes(d.deviceId) && !/broadcast|steam|virtual/i.test(d.label));
      if (!device && physical && /broadcast|steam/i.test(stream.getAudioTracks()[0].label)) {
        stream.getTracks().forEach(t => t.stop());
        stream = await this.acquire(constraints(physical.deviceId));
      }
      if (generation !== this.generation) {stream.getTracks().forEach(t => t.stop()); return null;}
      this.stream = stream;
      context = new this.Context(); this.context = context;
      await context.audioWorklet.addModule('/microphone-worklet.js');
      if (generation !== this.generation) return null;
      this.source = context.createMediaStreamSource(stream);
      this.node = new AudioWorkletNode(context, 'microphone-capture');
      this.node.port.onmessage = event => {if (generation === this.generation) onFrame(event.data);};
      this.source.connect(this.node); this.node.connect(context.destination);
      stream.getAudioTracks()[0].onended = onEnded;
      await context.resume();
      if (generation !== this.generation) return null;
      return {devices, name: stream.getAudioTracks()[0].label, id: stream.getAudioTracks()[0].getSettings().deviceId};
    } catch (error) {
      stream?.getTracks().forEach(t => t.stop());
      if (context && context.state !== 'closed') await context.close();
      if (generation === this.generation) this.stop();
      throw error;
    }
  }
  stop() {
    this.generation++;
    this.stream?.getTracks().forEach(track => {track.onended = null; track.stop();}); this.stream = null;
    this.source?.disconnect(); this.source = null;
    if (this.node) {this.node.port.onmessage = null; this.node.disconnect(); this.node = null;}
    if (this.context && this.context.state !== 'closed') void this.context.close();
    this.context = null;
  }
}
