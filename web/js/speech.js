/** Speaking replies with the browser's own speech synthesis. */

export class Speaker {
  constructor(config = {}) {
    this.config = { enabled: true, rate: 1, voice: null, max_chars: 700, ...config };
    this.speaking = false;
  }

  static get supported() {
    return typeof speechSynthesis !== 'undefined';
  }

  get available() {
    return Speaker.supported && this.config.enabled;
  }

  voices() {
    return Speaker.supported ? speechSynthesis.getVoices() : [];
  }

  /** Speak text, resolving when it finishes (or immediately if disabled). */
  say(text) {
    if (!this.available) return Promise.resolve();
    const spoken = trim(text, this.config.max_chars);
    if (!spoken) return Promise.resolve();

    this.cancel();
    return new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(spoken);
      utterance.rate = this.config.rate;
      const voice = this.voices().find((candidate) => candidate.name === this.config.voice);
      if (voice) utterance.voice = voice;
      const finish = () => {
        this.speaking = false;
        resolve();
      };
      utterance.onend = finish;
      utterance.onerror = finish;
      this.speaking = true;
      speechSynthesis.speak(utterance);
    });
  }

  cancel() {
    if (Speaker.supported) speechSynthesis.cancel();
    this.speaking = false;
  }
}

function trim(text, maxChars) {
  const clean = (text || '').trim();
  if (clean.length <= maxChars) return clean;
  const cut = clean.slice(0, maxChars);
  return `${cut.slice(0, cut.lastIndexOf(' ')) || cut}...`;
}
