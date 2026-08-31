import test from 'node:test';
import assert from 'node:assert/strict';

import { SseDecoder } from '../../web/js/api.js';

test('decodes a complete event', () => {
  const decoder = new SseDecoder();
  const events = decoder.push('event: delta\ndata: {"text":"hi"}\n\n');
  assert.deepEqual(events, [{ event: 'delta', data: { text: 'hi' } }]);
});

test('waits for the blank line before emitting', () => {
  const decoder = new SseDecoder();
  assert.deepEqual(decoder.push('event: delta\ndata: {"text":"par'), []);
  assert.deepEqual(decoder.push('tial"}\n\n'), [{ event: 'delta', data: { text: 'partial' } }]);
});

test('handles several events in one chunk', () => {
  const decoder = new SseDecoder();
  const events = decoder.push(
    'event: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {"text":"b"}\n\nevent: done\ndata: {"text":"ab"}\n\n',
  );
  assert.deepEqual(events.map((e) => e.event), ['delta', 'delta', 'done']);
  assert.equal(events[2].data.text, 'ab');
});

test('ignores comments and unknown fields', () => {
  const decoder = new SseDecoder();
  const events = decoder.push(': keep-alive\nid: 7\nevent: done\ndata: {"text":"x"}\n\n');
  assert.deepEqual(events, [{ event: 'done', data: { text: 'x' } }]);
});

test('non-JSON data still comes through as text', () => {
  const [event] = new SseDecoder().push('event: error\ndata: boom\n\n');
  assert.deepEqual(event, { event: 'error', data: { text: 'boom' } });
});

test('an event with no data line is skipped', () => {
  assert.deepEqual(new SseDecoder().push('event: ping\n\n'), []);
});
