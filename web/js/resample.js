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

  /**
   * Resample one chunk. Returns a Float32Array at the target rate.
   *
   * Downsampling averages over the whole span each output sample covers,
   * rather than picking one point out of it. Point sampling is aliasing: at
   * 48k to 16k everything above 8 kHz folds back into the speech band, and
   * sibilants -- of which Russian has plenty -- turn into noise sitting on
   * top of the vowels. The average is a crude low-pass, but it is the
   * difference between recognizable speech and mush.
   */
  process(chunk) {
    if (this.fromRate === this.toRate) return chunk;
    const out = [];
    const span = Math.max(1, Math.floor(this.ratio));
    for (; this.position < chunk.length; this.position += this.ratio) {
      const start = Math.floor(this.position);
      let total = 0;
      let counted = 0;
      for (let i = start; i < start + span; i += 1) {
        const sample = i < 0 ? this.previous : chunk[Math.min(i, chunk.length - 1)];
        total += sample;
        counted += 1;
      }
      out.push(total / counted);
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
