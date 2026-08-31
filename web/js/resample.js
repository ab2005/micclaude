/**
 * Linear resampling from the audio hardware's rate to the 16 kHz Whisper wants.
 *
 * The browser picks the capture rate (usually 48 kHz) and gives it to us in
 * small chunks, so the resampler keeps its fractional read position and the
 * last sample of the previous chunk between calls.
 */

export class Resampler {
  constructor(fromRate, toRate) {
    this.fromRate = fromRate;
    this.toRate = toRate;
    this.ratio = fromRate / toRate;
    this.position = 0;
    this.previous = 0;
  }

  reset() {
    this.position = 0;
    this.previous = 0;
  }

  /** Resample one chunk. Returns a Float32Array at the target rate. */
  process(chunk) {
    if (this.fromRate === this.toRate) return chunk;
    const out = [];
    // Index -1 refers to the last sample of the previous chunk.
    for (; this.position < chunk.length; this.position += this.ratio) {
      const left = Math.floor(this.position);
      const weight = this.position - left;
      const a = left < 0 ? this.previous : chunk[left];
      const b = left + 1 < chunk.length ? chunk[left + 1] : chunk[chunk.length - 1];
      out.push(a * (1 - weight) + b * weight);
    }
    this.position -= chunk.length;
    this.previous = chunk[chunk.length - 1] ?? this.previous;
    return Float32Array.from(out);
  }
}

/** Splits a stream of arbitrary chunks into fixed-size frames. */
export class FrameBuffer {
  constructor(frameSize) {
    this.frameSize = frameSize;
    this.pending = new Float32Array(0);
  }

  reset() {
    this.pending = new Float32Array(0);
  }

  /** Returns however many whole frames the chunk completed. */
  push(chunk) {
    const merged = new Float32Array(this.pending.length + chunk.length);
    merged.set(this.pending);
    merged.set(chunk, this.pending.length);
    const frames = [];
    let offset = 0;
    while (merged.length - offset >= this.frameSize) {
      frames.push(merged.slice(offset, offset + this.frameSize));
      offset += this.frameSize;
    }
    this.pending = merged.slice(offset);
    return frames;
  }
}
