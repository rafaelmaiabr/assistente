// Downsample mono capture to 16 kHz. The output remains silent (no sidetone).
class MicrophoneProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0; this.sum = 0; this.count = 0; this.samples = [];
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) for (const value of channel) {
      this.sum += value; this.count++; this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.samples.push(this.sum / this.count);
        this.phase -= sampleRate; this.sum = 0; this.count = 0;
        if (this.samples.length === 320) {
          this.port.postMessage(new Float32Array(this.samples));
          this.samples = [];
        }
      }
    }
    return true;
  }
}
registerProcessor('microphone-capture', MicrophoneProcessor);
