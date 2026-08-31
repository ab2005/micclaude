import test from 'node:test';
import assert from 'node:assert/strict';

import { mergeSettings } from '../../web/js/store.js';

const DEFAULTS = {
  audio: { energy_threshold: 0.015, silence_ms: 700 },
  trigger: { wake_words: ['claude'], require_prefix: false },
  speech: { enabled: true, rate: 1 },
  contextLines: 6,
};

test('defaults survive when nothing is stored', () => {
  const merged = mergeSettings(DEFAULTS, {});
  assert.deepEqual(merged.trigger.wake_words, ['claude']);
  assert.equal(merged.contextLines, 6);
  assert.equal(merged.deviceId, null);
});

test('stored preferences win per key without dropping the rest', () => {
  const merged = mergeSettings(DEFAULTS, {
    audio: { silence_ms: 1200 },
    trigger: { wake_words: ['computer'] },
    contextLines: 0,
    deviceId: 'mic-2',
  });
  assert.equal(merged.audio.silence_ms, 1200);
  assert.equal(merged.audio.energy_threshold, 0.015, 'untouched keys keep the default');
  assert.deepEqual(merged.trigger.wake_words, ['computer']);
  assert.equal(merged.trigger.require_prefix, false);
  assert.equal(merged.contextLines, 0, 'zero is kept, not treated as missing');
  assert.equal(merged.deviceId, 'mic-2');
});
