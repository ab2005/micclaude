import test from 'node:test';
import assert from 'node:assert/strict';

import { DEFAULT_AUDIO, Segmenter, rms } from '../../web/js/segmenter.js';
import { encodeWav, floatToPcm16 } from '../../web/js/wav.js';
import { FrameBuffer, Resampler } from '../../web/js/resample.js';

const CONFIG = {
  ...DEFAULT_AUDIO,
  frame_ms: 30,
  silence_ms: 300,
  preroll_ms: 90,
  min_utterance_ms: 200,
  max_utterance_ms: 5000,
  start_frames: 2,
};

function tone(amplitude, config = CONFIG) {
  const count = Math.round((config.sample_rate * config.frame_ms) / 1000);
  const frame = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    frame[i] = amplitude * Math.sin((2 * Math.PI * 220 * i) / config.sample_rate);
  }
  return frame;
}

const SILENCE = tone(0);
const SPEECH = tone(0.5);

function feed(segmenter, frames) {
  const utterances = [];
  for (const frame of frames) {
    const utterance = segmenter.push(frame);
    if (utterance) utterances.push(utterance);
  }
  const tail = segmenter.flush();
  if (tail) utterances.push(tail);
  return utterances;
}

const repeat = (frame, count) => Array.from({ length: count }, () => frame);

test('rms measures level', () => {
  assert.equal(rms(new Float32Array(0)), 0);
  assert.ok(rms(SILENCE) < 0.001);
  assert.ok(rms(SPEECH) > 0.3);
});

test('two phrases separated by silence become two utterances', () => {
  const utterances = feed(new Segmenter(CONFIG), [
    ...repeat(SILENCE, 5),
    ...repeat(SPEECH, 10),
    ...repeat(SILENCE, 12),
    ...repeat(SPEECH, 10),
    ...repeat(SILENCE, 12),
  ]);
  assert.equal(utterances.length, 2);
  for (const utterance of utterances) assert.ok(utterance.durationMs > 300);
});

test('audio from before the trigger is kept', () => {
  const [utterance] = feed(new Segmenter(CONFIG), [
    ...repeat(SILENCE, 5),
    ...repeat(SPEECH, 10),
    ...repeat(SILENCE, 12),
  ]);
  // 3 frames of preroll (one still silence), 8 remaining speech frames, and
  // the 10 frames of silence that closed the utterance.
  assert.equal(Math.round(utterance.durationMs), 21 * 30);
  assert.ok(Math.abs(utterance.samples[0]) < 0.001);
});

test('a short blip is discarded', () => {
  const segmenter = new Segmenter({ ...CONFIG, min_utterance_ms: 2000 });
  assert.deepEqual(feed(segmenter, [...repeat(SPEECH, 4), ...repeat(SILENCE, 12)]), []);
});

test('a long monologue is cut at the cap', () => {
  const segmenter = new Segmenter({ ...CONFIG, max_utterance_ms: 600 });
  const utterances = feed(segmenter, repeat(SPEECH, 60));
  assert.ok(utterances.length >= 2);
  assert.ok(utterances[0].durationMs <= 600);
});

test('silence alone produces nothing', () => {
  assert.deepEqual(feed(new Segmenter(CONFIG), repeat(SILENCE, 40)), []);
});

test('flush closes an open utterance, reset drops it', () => {
  const segmenter = new Segmenter(CONFIG);
  repeat(SPEECH, 10).forEach((frame) => segmenter.push(frame));
  assert.ok(segmenter.isSpeaking);
  assert.ok(segmenter.flush());
  assert.equal(segmenter.isSpeaking, false);

  repeat(SPEECH, 10).forEach((frame) => segmenter.push(frame));
  segmenter.reset();
  assert.equal(segmenter.flush(), null);
});

test('settings can be changed mid-stream', () => {
  const segmenter = new Segmenter(CONFIG);
  segmenter.update({ energy_threshold: 0.9, silence_ms: 60 });
  assert.equal(segmenter.silenceFrames, 2);
  assert.deepEqual(feed(segmenter, [...repeat(SPEECH, 10), ...repeat(SILENCE, 5)]), []);
  segmenter.update({ energy_threshold: 0.01 });
  assert.equal(feed(segmenter, [...repeat(SPEECH, 20), ...repeat(SILENCE, 5)]).length, 1);
});

test('float samples become clamped 16-bit PCM', () => {
  const pcm = floatToPcm16(Float32Array.from([0, 1, -1, 2, -2, 0.5]));
  assert.deepEqual(Array.from(pcm), [0, 32767, -32768, 32767, -32768, 16383]);
});

test('encodeWav writes a valid mono 16 kHz header', () => {
  const buffer = encodeWav(Float32Array.from([0, 0.5, -0.5]), 16000);
  const view = new DataView(buffer);
  const text = (offset, length) =>
    String.fromCharCode(...new Uint8Array(buffer, offset, length));
  assert.equal(text(0, 4), 'RIFF');
  assert.equal(text(8, 4), 'WAVE');
  assert.equal(text(36, 4), 'data');
  assert.equal(view.getUint16(22, true), 1, 'channels');
  assert.equal(view.getUint32(24, true), 16000, 'sample rate');
  assert.equal(view.getUint16(34, true), 16, 'bits per sample');
  assert.equal(view.getUint32(40, true), 6, 'data size');
  assert.equal(buffer.byteLength, 44 + 6);
});

test('resampling 48 kHz to 16 kHz keeps duration and continuity', () => {
  const resampler = new Resampler(48000, 16000);
  let total = 0;
  for (let i = 0; i < 10; i += 1) {
    total += resampler.process(new Float32Array(480)).length;
  }
  assert.equal(total, 1600); // 100 ms at 16 kHz
});

test('resampling is a no-op at matching rates', () => {
  const chunk = Float32Array.from([0.1, 0.2]);
  assert.equal(new Resampler(16000, 16000).process(chunk), chunk);
});

test('the frame buffer emits fixed-size frames and keeps the remainder', () => {
  const buffer = new FrameBuffer(4);
  assert.deepEqual(buffer.push(Float32Array.from([1, 2, 3])), []);
  const frames = buffer.push(Float32Array.from([4, 5, 6, 7, 8]));
  assert.equal(frames.length, 2);
  assert.deepEqual(Array.from(frames[0]), [1, 2, 3, 4]);
  assert.deepEqual(Array.from(frames[1]), [5, 6, 7, 8]);
  assert.equal(buffer.pending.length, 0);
});
