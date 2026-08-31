/**
 * Audio worklet: forwards captured mono audio to the main thread.
 *
 * The worklet runs on the audio thread in 128-sample quanta, which is far too
 * chatty for postMessage, so samples are batched before being sent.
 */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.batchSize = options?.processorOptions?.batchSize || 1024;
    this.buffer = new Float32Array(this.batchSize);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i += 1) {
      this.buffer[this.filled] = channel[i];
      this.filled += 1;
      if (this.filled === this.batchSize) {
        this.port.postMessage(this.buffer.slice(0));
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
