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

/*
 * Russian, as configured by `micclaude --lang ru`. Kept in step with
 * server/micclaude/languages.py by test_languages.py.
 */
const RUSSIAN = {
  wake_words: ['клавдий'],
  aliases: ['клавдия', 'клавдию', 'клавдие', 'клавдии', 'клаудий', 'клаудио', 'клавдей', 'клод', 'claude'],
  prefixes: ['эй', 'ок', 'окей', 'привет', 'слушай', 'слышь', 'хэй'],
  cancel_phrases: ['отмена', 'отменить', 'неважно', 'не важно', 'забудь', 'забей', 'отбой', 'проехали'],
  filler: ['пожалуйста', 'слушай', 'короче', 'ну', 'эм', 'э'],
};

test('Russian: a question after the name is an ask', () => {
  const result = new TriggerMatcher(RUSSIAN).match('Клавдий, почему падает сборка?');
  assert.equal(result.kind, TriggerKind.ASK);
  assert.equal(result.prompt, 'почему падает сборка?');
});

test('Russian: greetings and case forms still address it', () => {
  const matcher = new TriggerMatcher(RUSSIAN);
  for (const text of ['эй Клавдий что это', 'привет клавдий что это', 'клавдия что это']) {
    assert.equal(matcher.match(text).kind, TriggerKind.ASK, text);
  }
});

test('Russian: "код" and "клон" are not the wake word', () => {
  const matcher = new TriggerMatcher(RUSSIAN);
  for (const text of [
    'код не компилируется',
    'клон репозитория занял час',
    'клоун какой-то',
    'я вчера спрашивал Клавдия про это',
  ]) {
    assert.equal(matcher.match(text).kind, TriggerKind.NONE, text);
  }
});

test('Russian: the Latin spelling Whisper sometimes emits still counts', () => {
  assert.equal(new TriggerMatcher(RUSSIAN).match('Claude, что это?').kind, TriggerKind.ASK);
});

test('Russian: a bare name arms, and cancel phrases cancel', () => {
  const matcher = new TriggerMatcher(RUSSIAN);
  assert.equal(matcher.match('Клавдий').kind, TriggerKind.ARM);
  assert.equal(matcher.match('отмена').kind, TriggerKind.CANCEL);
  assert.equal(matcher.match('забудь').kind, TriggerKind.CANCEL);
});

test('Russian: pleasantries are dropped from the question', () => {
  const matcher = new TriggerMatcher(RUSSIAN);
  assert.equal(matcher.match('Клавдий, пожалуйста, покажи логи').prompt, 'покажи логи');
  assert.equal(matcher.match('Клавдий, ну покажи логи').prompt, 'покажи логи');
});

test('a short wake word is never widened by edit distance', () => {
  // "клод" is one edit from "код"; only the listed forms may match.
  const matcher = new TriggerMatcher({ wake_words: ['клод'], aliases: ['клода'], filler: [] });
  assert.equal(matcher.match('клод, что это').kind, TriggerKind.ASK);
  assert.equal(matcher.match('клода, что это').kind, TriggerKind.ASK);
  assert.equal(matcher.match('код не компилируется').kind, TriggerKind.NONE);
});

test('fuzzy_min_length decides which wake words are widened', () => {
  const strict = new TriggerMatcher({ wake_words: ['клод'], aliases: [], fuzzy_min_length: 4 });
  assert.equal(strict.match('код не компилируется').kind, TriggerKind.ASK, 'the collision this guards against');
  const safe = new TriggerMatcher({ wake_words: ['клод'], aliases: [], fuzzy_min_length: 6 });
  assert.equal(safe.match('код не компилируется').kind, TriggerKind.NONE);
});
