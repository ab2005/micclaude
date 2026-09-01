/**
 * Wiring: microphone -> transcription -> wake word -> Claude -> speech.
 *
 * The transcript lives here, in the page. The server transcribes audio and
 * relays questions; deciding whether a sentence was addressed to Claude
 * happens locally, so the feedback is immediate.
 */

import * as api from './api.js';
import { Capture, listInputDevices } from './capture.js';
import { LANGUAGES, applyTranslations, createTranslator, resolveLanguage } from './i18n.js';
import { Speaker } from './speech.js';
import { TriggerKind, TriggerMatcher } from './trigger.js';
import { encodeWav } from './wav.js';
import { loadPreferences, mergeSettings, savePreferences } from './store.js';

const el = (id) => document.getElementById(id);

const dom = {
  status: el('status'),
  statusText: el('status-text'),
  meterFill: el('meter-fill'),
  meterThreshold: el('meter-threshold'),
  listen: el('listen'),
  askNow: el('ask-now'),
  settingsToggle: el('settings-toggle'),
  notesToggle: el('notes-toggle'),
  rail: el('rail'),
  tabs: [...document.querySelectorAll('.tab')],
  panels: {
    notes: el('panel-notes'),
    settings: el('panel-settings'),
  },
  notesBody: el('notes-body'),
  notesStatus: el('notes-status'),
  notesTitle: el('notes-title'),
  finishMeeting: el('finish-meeting'),
  clearNotes: el('clear-notes'),
  feed: el('feed'),
  empty: el('empty'),
  facts: el('facts'),
  backendTag: el('backend-tag'),
  footnote: el('footnote'),
  composeForm: el('compose-form'),
  composeInput: el('compose-input'),
  newSession: el('new-session'),
  fields: {
    device: el('device'),
    threshold: el('threshold'),
    thresholdValue: el('threshold-value'),
    silence: el('silence'),
    silenceValue: el('silence-value'),
    wake: el('wake'),
    requirePrefix: el('require-prefix'),
    speak: el('speak'),
    voice: el('voice'),
    rate: el('rate'),
    rateValue: el('rate-value'),
    context: el('context'),
    uiLanguage: el('ui-language'),
  },
};

const state = {
  settings: null,
  language: 'en',
  t: createTranslator('en'),
  preferences: loadPreferences(),
  matcher: null,
  speaker: null,
  capture: null,
  transcript: [],
  notes: null,
  panel: 'notes',
  armed: false,
  busy: false,
  listening: false,
};

/* ------------------------------------------------------------------ status */

function setStatus(stateName) {
  dom.status.dataset.state = ['thinking', 'working'].includes(stateName) ? 'working' : stateName;
  dom.statusText.textContent = state.t(`status.${stateName}`);
}

function idleStatus() {
  if (state.busy) return;
  if (state.armed) setStatus('armed');
  else setStatus(state.listening ? 'listening' : 'idle');
}

/* -------------------------------------------------------------------- feed */

function scrollToEnd() {
  dom.feed.scrollTop = dom.feed.scrollHeight;
}

function record(text) {
  state.transcript.push({ time: new Date(), text });
  if (state.transcript.length > 200) state.transcript.shift();
}

function addHeard(text, source = 'mic') {
  dom.empty?.remove();
  const row = document.createElement('div');
  row.className = 'heard';
  if (source !== 'mic') row.dataset.source = source;
  const stamp = document.createElement('time');
  const now = new Date();
  stamp.dateTime = now.toISOString();
  stamp.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const body = document.createElement('span');
  body.textContent = text;
  row.append(stamp, body);
  dom.feed.append(row);
  scrollToEnd();
  record(text);
}

function addNotice(text, kind = 'info') {
  dom.empty?.remove();
  const notice = document.createElement('p');
  notice.className = 'notice';
  notice.dataset.kind = kind;
  notice.textContent = text;
  dom.feed.append(notice);
  scrollToEnd();
  return notice;
}

/** Something a standing instruction told the observer to watch for. */
function addFlag(flag) {
  dom.empty?.remove();
  const card = document.createElement('div');
  card.className = 'notice';
  card.dataset.kind = 'flag';

  const label = document.createElement('b');
  label.textContent = flag.rule ? state.t('notice.flag', { rule: flag.rule }) : flag.text;
  const body = document.createElement('div');
  body.textContent = flag.rule ? flag.text : '';
  card.append(label, body);

  if (flag.quote) {
    const quote = document.createElement('q');
    quote.textContent = flag.quote;
    card.append(quote);
  }
  dom.feed.append(card);
  scrollToEnd();
}

/** Render a question card whose answer fills in as it streams. */
function addExchange(question) {
  dom.empty?.remove();
  const card = document.createElement('article');
  card.className = 'exchange';

  const questionEl = document.createElement('div');
  questionEl.className = 'question';
  questionEl.textContent = question;

  const answerEl = document.createElement('div');
  answerEl.className = 'answer cursor';

  const metaEl = document.createElement('div');
  metaEl.className = 'meta';

  card.append(questionEl, answerEl, metaEl);
  dom.feed.append(card);
  scrollToEnd();

  let text = '';
  return {
    append(chunk) {
      text += chunk;
      answerEl.textContent = text;
      scrollToEnd();
    },
    finish(reply) {
      answerEl.classList.remove('cursor');
      if (reply.isError) card.dataset.error = 'true';
      if (!text) answerEl.textContent = reply.text || state.t('notice.noAnswer');
      const facts = [];
      if (reply.durationMs) facts.push(`${(reply.durationMs / 1000).toFixed(1)}s`);
      if (reply.costUsd) facts.push(`$${reply.costUsd.toFixed(3)}`);
      metaEl.textContent = facts.join(' · ');
      return answerEl.textContent;
    },
  };
}

/* --------------------------------------------------------------- pipeline */

function contextLines() {
  const count = state.settings.contextLines;
  if (!count) return [];
  return state.transcript
    .slice(-count - 1, -1)
    .map(({ time, text }) => `[${time.toLocaleTimeString([], { hour12: false })}] ${text}`);
}

/**
 * Route one utterance.
 *
 * `source` says where it came from: 'mic' for this page's own microphone,
 * 'typed' for the compose box (where the question card already shows the
 * text, so the transcript keeps it without a row), or the name a recorder
 * gave itself. That name is carried through to the row, so a setup with two
 * inputs can tell "you" from "the other end".
 */
async function handleText(text, { forceAsk = false, source = 'mic' } = {}) {
  const clean = (text || '').trim();
  if (!clean) return;

  if (source === 'typed') record(clean);
  else addHeard(clean, source);

  if (forceAsk) return ask(clean);

  if (state.armed) {
    setArmed(false);
    if (state.matcher.isCancel(clean)) {
      addNotice(state.t('notice.cancelled'));
      return undefined;
    }
    return ask(clean);
  }

  const result = state.matcher.match(clean);
  if (result.kind === TriggerKind.ASK) return ask(result.prompt);
  if (result.kind === TriggerKind.ARM) {
    setArmed(true);
    return undefined;
  }
  if (result.kind === TriggerKind.CANCEL) state.speaker.cancel();
  return undefined;
}

async function ask(question) {
  const exchange = addExchange(question);
  const context = contextLines();
  state.busy = true;
  state.capture?.setMuted(true);
  setStatus('thinking');
  try {
    const reply = await api.ask({ question, context }, { onDelta: (chunk) => exchange.append(chunk) });
    const spoken = exchange.finish(reply);
    if (!reply.isError && state.speaker.available) {
      setStatus('speaking');
      await state.speaker.say(spoken);
    }
  } catch (error) {
    exchange.finish({ text: String(error.message || error), isError: true });
  } finally {
    state.busy = false;
    state.capture?.setMuted(false);
    idleStatus();
    refreshFacts();
  }
}

async function handleUtterance(utterance) {
  if (state.busy) return;
  setStatus('working');
  try {
    const wav = encodeWav(utterance.samples, utterance.sampleRate);
    const { text } = await api.transcribe(wav);
    if (text) await handleText(text);
    else idleStatus();
  } catch (error) {
    addNotice(String(error.message || error), 'error');
    setStatus('error');
  } finally {
    if (!state.busy) idleStatus();
  }
}

function setArmed(armed) {
  state.armed = armed;
  dom.askNow.dataset.active = String(armed);
  dom.askNow.textContent = state.t(armed ? 'button.cancel' : 'button.ask');
  idleStatus();
}

/* ---------------------------------------------------------------- capture */

async function startListening() {
  if (!Capture.supported) {
    addNotice(state.t('error.noCapture'), 'error');
    return;
  }
  state.capture = new Capture({
    audio: state.settings.audio,
    onUtterance: handleUtterance,
    onLevel: updateMeter,
  });
  try {
    await state.capture.start(state.settings.deviceId || undefined);
  } catch (error) {
    state.capture = null;
    addNotice(
      error.name === 'NotAllowedError'
        ? state.t('error.micDenied')
        : state.t('error.micFailed', { error: error.message }),
      'error',
    );
    setStatus('error');
    return;
  }
  state.listening = true;
  dom.listen.textContent = state.t('button.stopListening');
  dom.listen.dataset.active = 'true';
  dom.askNow.disabled = false;
  idleStatus();
  await populateDevices();
}

async function stopListening() {
  await state.capture?.stop();
  state.capture = null;
  state.listening = false;
  setArmed(false);
  dom.listen.textContent = state.t('button.listen');
  dom.listen.dataset.active = 'false';
  dom.askNow.disabled = true;
  updateMeter(0, false);
  idleStatus();
}

function updateMeter(level, speaking) {
  const scaled = Math.min(1, level / 0.3);
  dom.meterFill.style.width = `${scaled * 100}%`;
  if (speaking && !state.busy) setStatus('hearing');
  else if (!state.busy && state.listening && dom.status.dataset.state === 'hearing') idleStatus();
}

/* --------------------------------------------------------------- settings */

function applySettings({ persist = true } = {}) {
  state.matcher = new TriggerMatcher(state.settings.trigger);
  state.speaker = state.speaker || new Speaker(state.settings.speech);
  state.speaker.config = { ...state.speaker.config, ...state.settings.speech };
  state.capture?.segmenter.update(state.settings.audio);
  dom.meterThreshold.style.insetInlineStart = `${
    Math.min(1, state.settings.audio.energy_threshold / 0.3) * 100
  }%`;
  translate();
  if (persist) savePreferences(state.preferences);
}

/* ---------------------------------------------------------------- language */

/** Re-render every string in the page for the current language. */
function translate() {
  const { wake_words: wake, cancel_phrases: cancel } = state.settings.trigger;
  applyTranslations(document, state.t, {
    // Wake words are matched lowercased; in a sentence it is a name.
    wake: capitalize(wake[0] || 'claude'),
    cancel: cancel[0] || 'never mind',
  });
  document.documentElement.lang = state.language;
  // Elements whose text depends on state, not just the language.
  dom.listen.textContent = state.t(state.listening ? 'button.stopListening' : 'button.listen');
  dom.askNow.textContent = state.t(state.armed ? 'button.cancel' : 'button.ask');
  if (refreshFacts.health) {
    refreshFacts();
    refreshFootnote();
  }
  if (state.notes) renderNotes();
  showValues();
  idleStatus();
}

/** Say where the audio goes and whether the text is kept. */
function refreshFootnote() {
  const health = refreshFacts.health;
  const parts = [state.t(health?.backend === 'openai' ? 'footnote.cloud' : 'footnote.local')];
  if (health?.transcriptPath) parts.push(state.t('footnote.saved'));
  dom.footnote.textContent = parts.join(' ');
}

function capitalize(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function setLanguage(code) {
  state.language = code;
  state.t = createTranslator(code);
  translate();
  // These are built in JavaScript, so they need rebuilding rather than
  // re-translating in place.
  populateLanguages();
  populateVoices();
  populateDevices();
}

function populateLanguages() {
  const select = dom.fields.uiLanguage;
  select.textContent = '';
  select.append(new Option(state.t('settings.uiLanguage.auto'), ''));
  for (const [code, label] of Object.entries(LANGUAGES)) select.append(new Option(label, code));
  select.value = state.preferences.uiLanguage || '';
}

function bindSettings() {
  const { fields } = dom;
  const { audio, trigger, speech } = state.settings;

  fields.threshold.value = audio.energy_threshold;
  fields.silence.value = audio.silence_ms;
  fields.wake.value = trigger.wake_words.join(', ');
  fields.requirePrefix.checked = trigger.require_prefix;
  fields.speak.checked = speech.enabled;
  fields.rate.value = speech.rate;
  fields.context.value = state.settings.contextLines;
  showValues();

  const update = (section, patch) => {
    Object.assign(state.settings[section], patch);
    state.preferences[section] = { ...state.preferences[section], ...patch };
    applySettings();
    showValues();
  };

  fields.threshold.addEventListener('input', () =>
    update('audio', { energy_threshold: Number(fields.threshold.value) }));
  fields.silence.addEventListener('input', () =>
    update('audio', { silence_ms: Number(fields.silence.value) }));
  fields.wake.addEventListener('change', () => {
    const words = fields.wake.value.split(',').map((word) => word.trim()).filter(Boolean);
    if (words.length === 0) {
      fields.wake.value = state.settings.trigger.wake_words.join(', ');
      return;
    }
    update('trigger', { wake_words: words });
  });
  fields.requirePrefix.addEventListener('change', () =>
    update('trigger', { require_prefix: fields.requirePrefix.checked }));
  fields.speak.addEventListener('change', () => {
    if (!fields.speak.checked) state.speaker.cancel();
    update('speech', { enabled: fields.speak.checked });
  });
  fields.voice.addEventListener('change', () => update('speech', { voice: fields.voice.value || null }));
  fields.rate.addEventListener('input', () => update('speech', { rate: Number(fields.rate.value) }));
  fields.context.addEventListener('change', () => {
    const value = Math.max(0, Math.min(40, Number(fields.context.value) || 0));
    fields.context.value = value;
    state.settings.contextLines = value;
    state.preferences.contextLines = value;
    applySettings();
  });
  fields.uiLanguage.addEventListener('change', () => {
    state.preferences.uiLanguage = fields.uiLanguage.value || null;
    savePreferences(state.preferences);
    setLanguage(resolveLanguage({
      stored: state.preferences.uiLanguage,
      server: state.settings.language,
      navigatorLanguages: navigator.languages,
    }));
  });

  fields.device.addEventListener('change', async () => {
    state.settings.deviceId = fields.device.value || null;
    state.preferences.deviceId = state.settings.deviceId;
    applySettings();
    if (state.listening) {
      await stopListening();
      await startListening();
    }
  });
}

function showValues() {
  const { fields } = dom;
  fields.thresholdValue.textContent = Number(fields.threshold.value).toFixed(3);
  fields.silenceValue.textContent = `${fields.silence.value} ${state.t('units.ms')}`;
  fields.rateValue.textContent = `${Number(fields.rate.value).toFixed(2)}×`;
}

async function populateDevices() {
  const devices = await listInputDevices();
  const select = dom.fields.device;
  const chosen = state.settings.deviceId || '';
  select.textContent = '';
  const fallback = new Option(state.t('settings.device.default'), '');
  select.append(fallback);
  devices.forEach((device, index) => {
    select.append(new Option(device.label || `${state.t('settings.microphone')} ${index + 1}`, device.deviceId));
  });
  select.value = devices.some((device) => device.deviceId === chosen) ? chosen : '';
}

function populateVoices() {
  const select = dom.fields.voice;
  const voices = state.speaker.preferredVoices();
  if (voices.length === 0) return;
  const chosen = state.settings.speech.voice || '';
  select.textContent = '';
  select.append(new Option(state.t('settings.voice.default'), ''));
  voices.forEach((voice) => select.append(new Option(`${voice.name} (${voice.lang})`, voice.name)));
  select.value = voices.some((voice) => voice.name === chosen) ? chosen : '';
}

function refreshFacts(health) {
  if (health) refreshFacts.health = health;
  const facts = refreshFacts.health;
  if (!facts) return;
  dom.facts.textContent = '';
  const rows = [
    [state.t('facts.model'), `${facts.backend} · ${facts.model}`],
    [state.t('facts.workingDir'), facts.workingDir],
    [state.t('facts.claudeModel'), facts.claudeModel || state.t('facts.default')],
    [state.t('facts.transcript'), facts.transcriptPath || state.t('facts.transcriptOff')],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    dom.facts.append(dt, dd);
  }
}

/* ------------------------------------------------------------------- boot */

/** Open the rail on a panel, or close it. */
function showRail(panel) {
  const open = Boolean(panel);
  dom.rail.hidden = !open;
  if (open) state.panel = panel;
  for (const [name, node] of Object.entries(dom.panels)) {
    node.hidden = !open || name !== state.panel;
  }
  for (const tab of dom.tabs) {
    tab.setAttribute('aria-selected', String(open && tab.dataset.panel === state.panel));
  }
  dom.settingsToggle.setAttribute('aria-expanded', String(open && state.panel === 'settings'));
  dom.notesToggle.setAttribute('aria-expanded', String(open && state.panel === 'notes'));
  if (open && state.panel === 'notes') refreshNotes();
}

/** Clicking the header button opens that panel, or closes an open one. */
function toggleRail(panel) {
  const showing = dom.rail.hidden || state.panel !== panel;
  showRail(showing ? panel : null);
  state.preferences.rail = showing ? panel : null;
  savePreferences(state.preferences);
}

/* -------------------------------------------------------------------- notes */

const NOTE_SECTIONS = ['points', 'decisions', 'tasks', 'questions', 'flags'];

async function refreshNotes() {
  try {
    state.notes = await api.getNotes();
  } catch (error) {
    dom.notesStatus.textContent = String(error.message || error);
    return;
  }
  renderNotes();
}

function renderNotes() {
  const payload = state.notes;
  if (!payload) return;
  const { notes, pending, enabled } = payload;

  dom.notesTitle.textContent = notes.title || state.t('notes.title');
  dom.notesStatus.textContent = !enabled
    ? state.t('notes.off')
    : pending
      ? state.t('notes.pending', { count: pending })
      : '';
  dom.finishMeeting.disabled = !enabled;

  dom.notesBody.textContent = '';
  let empty = true;
  for (const name of NOTE_SECTIONS) {
    const entries = notes[name] || [];
    if (entries.length === 0) continue;
    empty = false;

    const section = document.createElement('section');
    section.className = 'notes-section';
    const heading = document.createElement('h3');
    heading.textContent = state.t(`notes.section.${name}`);
    const list = document.createElement('ul');

    for (const entry of entries) {
      const item = document.createElement('li');
      const line = document.createElement('span');
      const who = entry.who ? `${entry.who} — ` : '';
      const due = entry.due ? ` (${entry.due})` : '';
      line.textContent = `${who}${entry.text}${due}`;
      item.append(line);
      if (entry.quote) {
        const quote = document.createElement('q');
        quote.textContent = entry.quote;
        item.append(quote);
      }
      list.append(item);
    }
    section.append(heading, list);
    dom.notesBody.append(section);
  }

  if (empty) {
    const nothing = document.createElement('p');
    nothing.className = 'notes-empty';
    nothing.textContent = state.t(enabled ? 'notes.empty' : 'notes.off');
    dom.notesBody.append(nothing);
  }
}

async function finishMeeting() {
  dom.finishMeeting.disabled = true;
  dom.notesStatus.textContent = state.t('notes.working');
  try {
    const result = await api.finishMeeting();
    if (result.summary) addExchange(state.t('button.finish')).finish({ text: result.summary });
    addNotice(state.t('notes.finished', { path: result.path || '—' }));
  } catch (error) {
    addNotice(String(error.message || error), 'error');
  } finally {
    dom.finishMeeting.disabled = false;
    await refreshNotes();
  }
}

/**
 * Listen to the server's event stream.
 *
 * Speech recognized by a separate recorder arrives here, so the page shows the
 * whole conversation even when it is not the one holding the microphone. Our
 * own utterances come back too -- those are already on screen, so they are
 * skipped by client id.
 */
function subscribeToServer() {
  api.subscribe({
    onUtterance(entry) {
      if (entry.client === api.clientId) return;
      handleText(entry.text, { source: entry.source || 'recorder' });
    },
    onFlag(flag) {
      addFlag(flag);
    },
    onNotes(payload) {
      state.notes = { ...(state.notes || {}), counts: payload.counts };
      if (!dom.rail.hidden && state.panel === 'notes') refreshNotes();
    },
    onSay(payload) {
      // Only the observer decides to interrupt, and only when a standing
      // instruction asked it to.
      if (state.settings.observer?.speakFlags && state.speaker.available) {
        state.speaker.say(payload.text);
      }
      addNotice(payload.text, 'flag');
    },
  });
}

function bindControls() {
  dom.listen.addEventListener('click', () =>
    (state.listening ? stopListening() : startListening()));

  dom.askNow.addEventListener('click', () => setArmed(!state.armed));

  dom.settingsToggle.addEventListener('click', () => toggleRail('settings'));
  dom.notesToggle.addEventListener('click', () => toggleRail('notes'));
  for (const tab of dom.tabs) {
    tab.addEventListener('click', () => {
      state.panel = tab.dataset.panel;
      showRail(state.panel);
      state.preferences.rail = state.panel;
      savePreferences(state.preferences);
    });
  }

  dom.finishMeeting.addEventListener('click', finishMeeting);
  dom.clearNotes.addEventListener('click', async () => {
    await api.clearNotes();
    addNotice(state.t('notes.cleared'));
    await refreshNotes();
  });

  dom.newSession.addEventListener('click', async () => {
    await api.resetSession();
    addNotice(state.t('notice.newSession'));
    refreshFacts(await api.getHealth());
  });

  dom.composeForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const question = dom.composeInput.value.trim();
    if (!question) return;
    dom.composeInput.value = '';
    handleText(question, { forceAsk: true, source: 'typed' });
  });

  document.addEventListener('keydown', (event) => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName);
    if (event.code === 'Space' && !typing && state.listening) {
      event.preventDefault();
      setArmed(!state.armed);
    }
    if (event.key === 'Escape') {
      state.speaker.cancel();
      setArmed(false);
    }
  });

  if (Speaker.supported) speechSynthesis.addEventListener?.('voiceschanged', populateVoices);
}

async function boot() {
  try {
    const defaults = await api.getSettings();
    state.settings = mergeSettings(defaults, state.preferences);
  } catch (error) {
    addNotice(state.t('error.server', { error: error.message }), 'error');
    setStatus('error');
    return;
  }
  state.language = resolveLanguage({
    stored: state.preferences.uiLanguage,
    server: state.settings.language,
    navigatorLanguages: navigator.languages,
  });
  state.t = createTranslator(state.language);
  state.speaker = new Speaker(state.settings.speech);
  applySettings({ persist: false });
  bindSettings();
  bindControls();
  showRail(state.preferences.rail || null);
  populateLanguages();
  populateVoices();
  subscribeToServer();
  await populateDevices();

  try {
    const health = await api.getHealth();
    refreshFacts(health);
    refreshFootnote();
    dom.backendTag.textContent = state.t(health.backend === 'openai' ? 'tag.cloudStt' : 'tag.localStt');
    if (health.claudeError) addNotice(health.claudeError, 'error');
  } catch (error) {
    addNotice(state.t('error.health', { error: error.message }), 'error');
  }
  idleStatus();
}

boot();
