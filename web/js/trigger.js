/**
 * Deciding when the speaker is actually addressing Claude.
 *
 * The microphone hears everything, but Claude should only answer when
 * explicitly asked. A request is recognized when a wake word appears near the
 * start of an utterance:
 *
 *   "hey claude, what does this mean"  -> ask, the prompt is the rest
 *   "claude"                           -> arm, the next utterance is the prompt
 *   "so I told claude about it"        -> none (wake word too late)
 *
 * Speech-to-text mangles names, so matching is fuzzy: "cloud", "clod" and
 * "claud" all count as "claude", while merely similar words do not.
 */

export const TriggerKind = Object.freeze({
  NONE: 'none',
  ASK: 'ask',
  ARM: 'arm',
  CANCEL: 'cancel',
});

export const DEFAULT_TRIGGER = Object.freeze({
  wake_words: ['claude'],
  aliases: ['cloud', 'claud', 'clawed', 'clod'],
  prefixes: ['hey', 'ok', 'okay', 'hi', 'hello', 'yo'],
  require_prefix: false,
  fuzzy: true,
  max_wake_distance: 1,
  scan_window_words: 4,
  cancel_phrases: ['never mind', 'nevermind', 'cancel that', 'forget it'],
  min_prompt_chars: 2,
});

const LEADING_FILLER = ['please', 'um', 'uh', 'so', 'well', 'hey', 'okay', 'ok'];
const ADDRESS_WORDS = new Set(['there', 'yo']);

/** Lowercase, strip accents and punctuation, collapse whitespace. */
export function normalize(text) {
  return (text || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s']/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Edit distance with an early exit once `maxDistance` is exceeded. */
export function levenshtein(a, b, maxDistance = Infinity) {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > maxDistance) return maxDistance + 1;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    const current = [i];
    let best = i;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      current[j] = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost);
      best = Math.min(best, current[j]);
    }
    if (best > maxDistance) return maxDistance + 1;
    previous = current;
  }
  return previous[b.length];
}

export class TriggerMatcher {
  constructor(config = {}) {
    this.config = { ...DEFAULT_TRIGGER, ...config };
    this.wake = (this.config.wake_words || []).map(normalize).filter(Boolean);
    if (this.wake.length === 0) throw new Error('at least one wake word is required');
    this.aliases = new Set((this.config.aliases || []).map(normalize).filter(Boolean));
    this.prefixes = new Set((this.config.prefixes || []).map(normalize));
    this.cancels = (this.config.cancel_phrases || []).map(normalize).filter(Boolean);
  }

  isCancel(text) {
    const normalized = normalize(text);
    return this.cancels.some((phrase) => normalized === phrase || normalized.startsWith(phrase));
  }

  /** Classify one utterance. Returns { kind, prompt, matched }. */
  match(text) {
    if (this.isCancel(text)) return { kind: TriggerKind.CANCEL, prompt: '', matched: '' };

    const words = normalize(text).split(' ').filter(Boolean);
    if (words.length === 0) return { kind: TriggerKind.NONE, prompt: '', matched: '' };

    const window = Math.min(words.length, Math.max(1, this.config.scan_window_words));
    for (let index = 0; index < window; index += 1) {
      const matched = this.wakeHit(words[index]);
      if (!matched) continue;
      if (this.config.require_prefix && !this.prefixes.has(words[index - 1])) continue;
      if (index > 0 && !this.isAddressing(words, index)) continue;
      const prompt = cleanPrompt(originalTail(text, words.length, index));
      if (prompt.length < this.config.min_prompt_chars) {
        return { kind: TriggerKind.ARM, prompt: '', matched };
      }
      return { kind: TriggerKind.ASK, prompt, matched };
    }
    return { kind: TriggerKind.NONE, prompt: '', matched: '' };
  }

  /**
   * Return the wake word this word stands for, or "".
   *
   * Configured aliases must match exactly; only the canonical wake words are
   * widened by edit distance. So "cloud" (a listed alias) triggers while the
   * merely similar "loud" does not.
   */
  wakeHit(word) {
    if (this.wake.includes(word)) return word;
    if (this.aliases.has(word)) return this.wake[0];
    if (this.config.fuzzy) {
      const budget = this.config.max_wake_distance;
      for (const wake of this.wake) {
        if (wake.length >= 4 && levenshtein(word, wake, budget) <= budget) return wake;
      }
    }
    return '';
  }

  /**
   * A late wake word only counts if everything before it is a greeting, so
   * "I asked claude about it" is not a request but "hey there claude" is.
   */
  isAddressing(words, index) {
    return words.slice(0, index).every((word) => this.prefixes.has(word) || ADDRESS_WORDS.has(word));
  }
}

/**
 * The part of the *original* text after word `index`. Normalization is only
 * used for matching, so the prompt keeps the casing and punctuation the
 * transcriber produced.
 */
function originalTail(text, wordCount, index) {
  const remaining = wordCount - index - 1;
  if (remaining <= 0) return '';
  const tokens = (text.match(/\S+/g) || []);
  return tokens.slice(Math.max(0, tokens.length - remaining)).join(' ');
}

function cleanPrompt(tail) {
  let cleaned = tail.trim().replace(/^[,.:;!?\-\s]+/, '').trim();
  for (;;) {
    const lowered = cleaned.toLowerCase();
    const filler = LEADING_FILLER.find((word) => lowered.startsWith(`${word} `));
    if (!filler) return cleaned;
    cleaned = cleaned.slice(filler.length + 1).replace(/^[,.:;!?\-\s]+/, '').trim();
  }
}
