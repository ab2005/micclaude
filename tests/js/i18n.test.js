import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FALLBACK_LANGUAGE,
  LANGUAGES,
  STRINGS,
  createTranslator,
  resolveLanguage,
} from '../../web/js/i18n.js';

test('every language offers the same keys', () => {
  const reference = Object.keys(STRINGS[FALLBACK_LANGUAGE]).sort();
  for (const [code, table] of Object.entries(STRINGS)) {
    assert.deepEqual(Object.keys(table).sort(), reference, `${code} has a different key set`);
  }
});

test('no translation is empty and none is left in English by accident', () => {
  for (const [code, table] of Object.entries(STRINGS)) {
    for (const [key, value] of Object.entries(table)) {
      assert.ok(value.trim().length > 0, `${code}.${key} is empty`);
    }
  }
  const untranslated = Object.keys(STRINGS.ru).filter(
    (key) => STRINGS.ru[key] === STRINGS.en[key] && /[A-Za-z]{4}/.test(STRINGS.en[key]),
  );
  assert.deepEqual(untranslated, [], 'these keys still hold the English text');
});

test('every language has a name to show in the picker', () => {
  assert.deepEqual(Object.keys(LANGUAGES).sort(), Object.keys(STRINGS).sort());
});

test('a stored choice wins, then the server, then the browser', () => {
  assert.equal(resolveLanguage({ stored: 'en', server: 'ru' }), 'en');
  assert.equal(resolveLanguage({ server: 'ru', navigatorLanguages: ['en-US'] }), 'ru');
  assert.equal(resolveLanguage({ navigatorLanguages: ['ru-RU', 'en'] }), 'ru');
  assert.equal(resolveLanguage({}), 'en');
});

test('unknown languages fall through to one we have', () => {
  assert.equal(resolveLanguage({ stored: 'kl', server: 'ru' }), 'ru');
  assert.equal(resolveLanguage({ navigatorLanguages: ['kl-GL'] }), FALLBACK_LANGUAGE);
});

test('placeholders are filled in', () => {
  const t = createTranslator('ru');
  assert.match(t('empty.hint.arm', { wake: 'Клавдий' }), /«Клавдий»/);
  assert.match(t('error.micFailed', { error: 'нет устройства' }), /нет устройства/);
});

test('an unknown placeholder is left alone rather than blanked', () => {
  assert.match(createTranslator('en')('error.micFailed', {}), /\{error\}/);
});

test('a missing key falls back to English, then to the key itself', () => {
  const t = createTranslator('ru');
  assert.equal(t('status.idle'), STRINGS.ru['status.idle']);
  assert.equal(t('nope.not.here'), 'nope.not.here');
});
