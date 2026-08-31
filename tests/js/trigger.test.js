import test from 'node:test';
import assert from 'node:assert/strict';

import { TriggerKind, TriggerMatcher, levenshtein, normalize } from '../../web/js/trigger.js';

test('normalize strips punctuation, case and accents', () => {
  assert.equal(normalize("  Hey, Claudé!  What's up? "), "hey claude what's up");
  assert.equal(normalize('   ...  '), '');
});

test('levenshtein measures and short-circuits', () => {
  assert.equal(levenshtein('claude', 'claude'), 0);
  assert.equal(levenshtein('claud', 'claude'), 1);
  assert.equal(levenshtein('cloud', 'claude'), 2);
  assert.ok(levenshtein('something', 'claude', 1) > 1);
});

test('a wake word plus a question is an ask', () => {
  const result = new TriggerMatcher().match('Hey Claude, why is the build red?');
  assert.equal(result.kind, TriggerKind.ASK);
  assert.equal(result.prompt, 'why is the build red?');
});

test('the prompt keeps its original casing', () => {
  assert.equal(new TriggerMatcher().match('claude open the README file').prompt, 'open the README file');
});

test('a bare wake word arms the next utterance', () => {
  assert.equal(new TriggerMatcher().match('Claude?').kind, TriggerKind.ARM);
});

test('ordinary speech is ignored', () => {
  const matcher = new TriggerMatcher();
  for (const text of ['the tests are failing', 'so I asked claude about it yesterday', '']) {
    assert.equal(matcher.match(text).kind, TriggerKind.NONE, text);
  }
});

test('greetings before the wake word still count', () => {
  const result = new TriggerMatcher().match('ok hey there claude what time is it');
  assert.equal(result.kind, TriggerKind.ASK);
  assert.equal(result.prompt, 'what time is it');
});

test('known mishearings trigger, similar words do not', () => {
  const matcher = new TriggerMatcher();
  for (const text of ['hey cloud what is this', 'clawed what is this', 'hey claud what is this']) {
    assert.equal(matcher.match(text).kind, TriggerKind.ASK, text);
  }
  for (const text of ['loud noises outside', 'claudia sent the patch', 'clouds today']) {
    assert.equal(matcher.match(text).kind, TriggerKind.NONE, text);
  }
});

test('cancel phrases are recognized', () => {
  const matcher = new TriggerMatcher();
  assert.equal(matcher.match('never mind').kind, TriggerKind.CANCEL);
  assert.ok(matcher.isCancel('Cancel that, please.'));
});

test('require_prefix demands a greeting', () => {
  const matcher = new TriggerMatcher({ require_prefix: true });
  assert.equal(matcher.match('claude do the thing').kind, TriggerKind.NONE);
  assert.equal(matcher.match('hey claude do the thing').kind, TriggerKind.ASK);
});

test('a custom wake word replaces the default', () => {
  const matcher = new TriggerMatcher({ wake_words: ['computer'], aliases: [] });
  assert.equal(matcher.match('computer, status report').kind, TriggerKind.ASK);
  assert.equal(matcher.match('claude, status report').kind, TriggerKind.NONE);
});

test('fuzzy matching can be turned off', () => {
  const matcher = new TriggerMatcher({ fuzzy: false, aliases: [] });
  assert.equal(matcher.match('hey claud what is this').kind, TriggerKind.NONE);
});

test('leading filler is dropped from the prompt', () => {
  assert.equal(new TriggerMatcher().match('claude, please summarize this').prompt, 'summarize this');
});

test('an empty wake word list is rejected', () => {
  assert.throws(() => new TriggerMatcher({ wake_words: [' '] }));
});
