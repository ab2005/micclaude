/** Speaking replies with the browser's own speech synthesis. */

export class Speaker {
  constructor(config = {}) {
    this.config = { enabled: true, rate: 1, lang: 'en-US', voice: null, max_chars: 700, ...config };
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

  /**
   * Voices for the reply language first, then the rest.
   *
   * Without this a Russian reply is read out by an English voice, which is
   * unintelligible rather than merely accented.
   */
  preferredVoices() {
    const base = String(this.config.lang || '').toLowerCase().split('-')[0];
    const matches = (voice) => voice.lang.toLowerCase().startsWith(base);
    const voices = this.voices();
    return [...voices.filter(matches), ...voices.filter((voice) => !matches(voice))];
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
      if (this.config.lang) utterance.lang = this.config.lang;
      const [best] = this.preferredVoices();
      const voice = this.voices().find((candidate) => candidate.name === this.config.voice) || best;
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
