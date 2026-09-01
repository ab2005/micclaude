/**
 * Microphone capture in the browser.
 *
 * getUserMedia -> AudioWorklet -> resample to 16 kHz -> fixed frames ->
 * segmenter -> whole utterances. The browser's echo cancellation and noise
 * suppression are left on: they make the energy-based speech detection far
 * more reliable, and they stop the page hearing its own spoken replies.
 */

import { Segmenter } from './segmenter.js';
import { FrameBuffer, Resampler } from './resample.js';

export class Capture {
  /**
   * @param {object} options
   * @param {object} options.audio        audio settings from /api/settings
   * @param {(u: object) => void} options.onUtterance  called with each phrase
   * @param {(level: number, speaking: boolean) => void} [options.onLevel]
   */
  constructor({ audio, onUtterance, onLevel }) {
    this.audio = audio;
    this.onUtterance = onUtterance;
    this.onLevel = onLevel;
    this.segmenter = new Segmenter(audio);
    this.frames = new FrameBuffer(this.segmenter.frameSamples);
    this.resampler = null;
    this.context = null;
    this.stream = null;
    this.node = null;
    this.muted = false;
    this.running = false;
  }

  static get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia && window.AudioContext);
  }

  async start(deviceId) {
    if (this.running) return;
    // Echo cancellation earns its place: without it the page transcribes its
    // own spoken replies. Noise suppression and automatic gain do not -- they
    // are tuned to keep a human on a phone call intelligible, and they take
    // the consonants with the noise. A speech model would rather have the hiss.
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: this.audio.echo_cancellation !== false,
        noiseSuppression: this.audio.noise_suppression === true,
        autoGainControl: this.audio.auto_gain === true,
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    });
    // Ask the browser for the rate we want. Its own resampler is properly
    // filtered; ours is not, and downsampling 48k to 16k without a low-pass
    // folds every sibilant back into the speech band as noise. Whisper hears
    // mush. Not every browser honours the request, so we check and only fall
    // back to our own when it did not.
    try {
      this.context = new AudioContext({ sampleRate: this.audio.sample_rate });
    } catch {
      this.context = new AudioContext();
    }
    await this.context.resume();
    this.resampled = this.context.sampleRate !== this.audio.sample_rate;
    if (this.resampled) {
      console.warn(
        `the browser gave ${this.context.sampleRate} Hz, not ${this.audio.sample_rate}; ` +
        'resampling in the page, which costs some accuracy',
      );
    }
    this.resampler = new Resampler(this.context.sampleRate, this.audio.sample_rate);
    const source = this.context.createMediaStreamSource(this.stream);

    try {
      await this.context.audioWorklet.addModule('/js/capture-worklet.js');
      this.node = new AudioWorkletNode(this.context, 'capture-processor', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: { batchSize: 1024 },
      });
      this.node.port.onmessage = (event) => this.handleChunk(event.data);
      source.connect(this.node);
    } catch (error) {
      // Older Safari and any browser that refuses the worklet module.
      console.warn('AudioWorklet unavailable, falling back to ScriptProcessor', error);
      this.node = this.context.createScriptProcessor(4096, 1, 1);
      this.node.onaudioprocess = (event) => this.handleChunk(event.inputBuffer.getChannelData(0));
      source.connect(this.node);
      this.node.connect(this.context.destination);
    }
    this.running = true;
  }

  /** Drop incoming audio without releasing the microphone. */
  setMuted(muted) {
    if (muted && !this.muted) {
      this.segmenter.reset();
      this.frames.reset();
      this.onLevel?.(0, false);
    }
    this.muted = muted;
  }

  handleChunk(chunk) {
    if (!this.running || this.muted) return;
    for (const frame of this.frames.push(this.resampler.process(chunk))) {
      const utterance = this.segmenter.push(frame);
      this.onLevel?.(this.segmenter.level, this.segmenter.isSpeaking);
      if (utterance) this.onUtterance(utterance);
    }
  }

  async stop() {
    if (!this.running) return;
    this.running = false;
    const tail = this.segmenter.flush();
    if (tail) this.onUtterance(tail);
    if (this.node) {
      this.node.disconnect();
      if (this.node.port) this.node.port.onmessage = null;
      this.node.onaudioprocess = null;
    }
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.context?.close();
    this.context = this.node = this.stream = null;
    this.onLevel?.(0, false);
  }
}

/** Input devices, once permission has been granted (labels are hidden before). */
export async function listInputDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((device) => device.kind === 'audioinput');
}
