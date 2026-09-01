/** Talking to the local micclaude server. */

/** Incremental parser for the server-sent-event stream returned by /api/ask. */
export class SseDecoder {
  constructor() {
    this.buffer = '';
  }

  /** Feed a chunk of text; returns the events it completed. */
  push(text) {
    this.buffer += text;
    const events = [];
    let index;
    while ((index = this.buffer.indexOf('\n\n')) !== -1) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      const event = parseEventBlock(block);
      if (event) events.push(event);
    }
    return events;
  }
}

function parseEventBlock(block) {
  let name = 'message';
  const data = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).trim());
    // Comment lines (":") and unknown fields are ignored, per the SSE spec.
  }
  if (data.length === 0) return null;
  try {
    return { event: name, data: JSON.parse(data.join('\n')) };
  } catch {
    return { event: name, data: { text: data.join('\n') } };
  }
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

export const getHealth = () => getJson('/api/health');
export const getSettings = () => getJson('/api/settings');
export const getTranscript = () => getJson('/api/transcript');
export const getNotes = () => getJson('/api/notes');

/** Send whatever the observer is holding right now. */
export async function flushNotes() {
  const response = await fetch('/api/notes/flush', { method: 'POST' });
  return response.json();
}

/**
 * This page's identity, so it can ignore the echo of its own utterances
 * coming back over the event stream.
 */
export const clientId = `page-${Math.random().toString(36).slice(2, 10)}`;

/**
 * Subscribe to everything the server recognizes, including speech that came
 * from a separate recorder process rather than this browser.
 *
 * Returns a function that unsubscribes. EventSource reconnects on its own, so
 * a server restart does not need a page reload.
 */
export function subscribe({ onUtterance, onFlag, onNotes, onSay, onOpen, onError } = {}) {
  const source = new EventSource('/api/events');
  const on = (name, handler) => source.addEventListener(name, (event) => {
    if (!handler) return;
    try {
      handler(JSON.parse(event.data));
    } catch (error) {
      console.warn(`bad ${name} event`, error);
    }
  });
  on('utterance', onUtterance);
  on('flag', onFlag);
  on('notes', onNotes);
  on('say', onSay);
  source.addEventListener('open', () => onOpen?.());
  source.addEventListener('error', () => onError?.());
  return () => source.close();
}

export async function resetSession() {
  await fetch('/api/session/reset', { method: 'POST' });
}

/** Upload one utterance as WAV; resolves to { text, audioMs, elapsedMs }. */
export async function transcribe(wav, { signal } = {}) {
  const response = await fetch('/api/transcribe', {
    method: 'POST',
    headers: { 'Content-Type': 'audio/wav', 'X-Client-Id': clientId },
    body: wav,
    signal,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `transcription failed (${response.status})`);
  return payload;
}

/**
 * Ask Claude and stream the reply.
 *
 * `onDelta` is called with each chunk of text as it arrives. Resolves with the
 * final result once the turn ends.
 */
export async function ask({ question, context = [] }, { onDelta, signal } = {}) {
  const response = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, context }),
    signal,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `ask failed (${response.status})`);
  }

  const decoder = new SseDecoder();
  const reader = response.body.getReader();
  const utf8 = new TextDecoder();
  let result = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const { event, data } of decoder.push(utf8.decode(value, { stream: true }))) {
      if (event === 'delta') onDelta?.(data.text || '');
      else if (event === 'done') result = { ...data, isError: false };
      else if (event === 'error') result = { ...data, isError: true };
    }
  }
  return result || { text: 'Claude closed the connection without answering.', isError: true };
}
