import test from 'node:test';
import assert from 'node:assert/strict';
import {SpeechGate, wavBase64, Microphone, microphoneError} from '../src/voice.js';

test('silence and short clicks do not create utterances', () => {
  const output=[]; const gate=new SpeechGate(x=>output.push(x),()=>{});
  for(let i=0;i<100;i++) gate.push(new Float32Array(320));
  gate.push(new Float32Array(320).fill(.1));
  for(let i=0;i<100;i++) gate.push(new Float32Array(320));
  assert.equal(output.length,0);
});
test('speech triggers interruption once and ends after silence', () => {
  const output=[]; let starts=0;
  const gate=new SpeechGate(x=>output.push(x),()=>starts++);
  for(let i=0;i<25;i++) gate.push(new Float32Array(320).fill(.1));
  for(let i=0;i<45;i++) gate.push(new Float32Array(320));
  assert.equal(starts,1); assert.equal(output.length,1);
  const wav=Buffer.from(wavBase64(output[0]),'base64');
  assert.equal(wav.toString('ascii',0,4),'RIFF'); assert.equal(wav.readUInt32LE(24),16000);
  assert.equal(wav.readUInt32LE(40),wav.length-44);
});
test('long speech is bounded and mute discards unfinished utterance', () => {
  const output=[]; const gate=new SpeechGate(x=>output.push(x),()=>{});
  for(let i=0;i<1100;i++) gate.push(new Float32Array(320).fill(.1));
  assert.equal(output.length,1); assert.ok(output[0].length*320<=320000);
  gate.reset(); for(let i=0;i<60;i++) gate.push(new Float32Array(320));
  assert.equal(output.length,1);
});
test('pending permission cannot reactivate a stopped microphone', async () => {
  let resolve, stopped=false;
  const media={getUserMedia:()=>new Promise(r=>resolve=r)};
  const mic=new Microphone(media,class{});
  const pending=mic.start('',()=>{},()=>{}); mic.stop();
  resolve({getTracks:()=>[{stop:()=>stopped=true}]});
  assert.equal(await pending,null); assert.equal(stopped,true);
});
test('missing and denied microphones have actionable errors', async () => {
  for(const name of ['NotFoundError','NotAllowedError']) {
    const mic=new Microphone({getUserMedia:async()=>{throw {name};}}, class{});
    await assert.rejects(mic.start('',()=>{},()=>{}),error=>microphoneError(error).length>30);
  }
});
test('permission timeout releases a stream that arrives late', async () => {
  let resolve, stopped=false;
  const mic=new Microphone({getUserMedia:()=>new Promise(r=>resolve=r)},class{},5);
  await assert.rejects(mic.start('',()=>{},()=>{}), /não liberou/);
  resolve({getTracks:()=>[{stop:()=>stopped=true}]});
  await new Promise(r=>setImmediate(r));
  assert.equal(stopped,true);
});
