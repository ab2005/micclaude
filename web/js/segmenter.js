/**
 * Grouping audio frames into utterances.
 *
 * A run of speech frames, closed by a stretch of silence, becomes one
 * utterance. Only whole phrases are sent to the transcriber, which is both
 * cheaper and far more accurate than transcribing fixed-size chunks.
 *
 * Speech detection is an RMS threshold. The browser's own noise suppression
 * does most of the heavy lifting before the audio reaches us.
 */

export const DEFAULT_AUDIO = Object.freeze({
  sample_rate: 16000,
  frame_ms: 30,
  energy_threshold: 0.015,
  start_frames: 3,
  silence_ms: 700,
  preroll_ms: 300,
  min_utterance_ms: 350,
  max_utterance_ms: 30000,
  echo_cancellation: true,
  noise_suppression: false,
  auto_gain: false,
});

/** Root-mean-square level of a float frame, already in 0..1. */
export function rms(frame) {
  if (!frame || frame.length === 0) return 0;
  let total = 0;
  for (let i = 0; i < frame.length; i += 1) total += frame[i] * frame[i];
  return Math.sqrt(total / frame.length);
}

export class Segmenter {
  constructor(config = {}) {
    this.update(config);
    this.reset();
  }

  /** Apply new settings. Frame size aside, these can change mid-stream. */
  update(config = {}) {
    this.config = { ...DEFAULT_AUDIO, ...this.config, ...config };
    this.frameSamples = Math.round((this.config.sample_rate * this.config.frame_ms) / 1000);
    this.prerollFrames = Math.max(1, Math.floor(this.config.preroll_ms / this.config.frame_ms));
    this.silenceFrames = Math.max(1, Math.floor(this.config.silence_ms / this.config.frame_ms));
    this.maxFrames = Math.max(1, Math.floor(this.config.max_utterance_ms / this.config.frame_ms));
  }

  reset() {
    this.preroll = [];
    this.voiced = [];
    this.speechRun = 0;
    this.silenceRun = 0;
    this.triggered = false;
    this.level = 0;
  }

  get isSpeaking() {
    return this.triggered;
  }

  /** Feed one frame; returns an utterance when one just completed. */
  push(frame) {
    this.level = rms(frame);
    const speech = this.level >= this.config.energy_threshold;

    if (!this.triggered) {
      this.preroll.push(frame);
      if (this.preroll.length > this.prerollFrames) this.preroll.shift();
      this.speechRun = speech ? this.speechRun + 1 : 0;
      if (this.speechRun >= this.config.start_frames) {
        this.triggered = true;
        this.voiced = this.preroll.slice();
        this.preroll = [];
        this.silenceRun = 0;
      }
      return null;
    }

    this.voiced.push(frame);
    this.silenceRun = speech ? 0 : this.silenceRun + 1;
    if (this.silenceRun >= this.silenceFrames || this.voiced.length >= this.maxFrames) {
      return this.close();
    }
    return null;
  }

  /** Close any in-progress utterance, e.g. when capture stops. */
  flush() {
    return this.triggered ? this.close() : null;
  }

  close() {
    const frames = this.voiced;
    this.reset();
    const length = frames.reduce((total, frame) => total + frame.length, 0);
    const samples = new Float32Array(length);
    let offset = 0;
    for (const frame of frames) {
      samples.set(frame, offset);
      offset += frame.length;
    }
    const durationMs = (samples.length / this.config.sample_rate) * 1000;
    if (durationMs < this.config.min_utterance_ms) return null;
    return { samples, sampleRate: this.config.sample_rate, durationMs };
  }
}
