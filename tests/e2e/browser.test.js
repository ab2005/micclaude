/**
 * End-to-end: a real Chromium drives the real page against a real server.
 *
 * Speech-to-text and the Claude CLI are stubbed by fixture_server.py, so the
 * test is deterministic, but everything between them -- capture, segmentation,
 * the WAV upload, wake-word matching, the SSE stream, the rendered answer --
 * is the shipping code.
 *
 * Chromium plays a generated WAV into the fake microphone -- a second of tone
 * followed by silence, looping -- so capture, level detection and the silence
 * boundary that closes an utterance are all exercised for real.
 *
 * Skipped when playwright is not installed:
 *   npm install -D playwright && npx playwright install chromium
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { encodeWav } from '../../web/js/wav.js';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../..');

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  test('browser end-to-end', { skip: 'playwright is not installed' }, () => {});
}

/**
 * A second of tone followed by 1.5s of silence, written where Chromium's
 * --use-file-for-fake-audio-capture can loop it into the fake microphone.
 */
function writeFakeMicAudio() {
  const rate = 16000;
  const samples = new Float32Array(rate * 2.5);
  for (let i = 0; i < rate; i += 1) samples[i] = 0.35 * Math.sin((2 * Math.PI * 220 * i) / rate);
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'micclaude-')), 'mic.wav');
  fs.writeFileSync(file, Buffer.from(encodeWav(samples, rate)));
  return file;
}

/** Start fixture_server.py and wait for it to print its URL. */
function startServer(env = {}) {
  const server = spawn('python3', [path.join(here, 'fixture_server.py')], {
    cwd: repoRoot,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...env },
  });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start in time')), 15000);
    server.stdout.on('data', (chunk) => {
      const url = String(chunk).trim().split('\n')[0];
      if (url.startsWith('http')) {
        clearTimeout(timer);
        resolve({ server, url });
      }
    });
    server.on('error', reject);
  });
}

if (chromium) {
  test('browser end-to-end', async (t) => {
    const { server, url } = await startServer();
    const browser = await chromium.launch({
      args: [
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        `--use-file-for-fake-audio-capture=${writeFakeMicAudio()}`,
        '--autoplay-policy=no-user-gesture-required',
      ],
    });
    const context = await browser.newContext({ permissions: ['microphone'] });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));

    t.after(async () => {
      await browser.close();
      server.kill();
    });

    await page.goto(url);

    await t.test('the page loads its settings from the server', async () => {
      await page.waitForSelector('#facts dd', { state: 'attached' });
      assert.equal(await page.textContent('#status-text'), 'Not listening');
      assert.match(await page.textContent('#facts'), /stub/);
      assert.equal(await page.inputValue('#wake'), 'claude');
    });

    await t.test('a typed question streams an answer back', async () => {
      await page.fill('#compose-input', 'what is the capital of France?');
      await page.click('#compose-form button[type=submit]');
      await page.waitForSelector('.exchange .answer:not(.cursor)');
      assert.equal(
        await page.textContent('.exchange .question'),
        'what is the capital of France?',
      );
      assert.match(
        await page.textContent('.exchange .answer'),
        /You asked: .*capital of France/,
      );
      assert.match(await page.textContent('.exchange .meta'), /0\.0s/);
    });

    await t.test('speaking triggers transcription, the wake word and an answer', async () => {
      await page.click('#listen');
      await page.waitForSelector('#ask-now:not([disabled])');
      // The fixture transcriber answers "hey claude, what is this?" for any audio.
      await page.waitForSelector('.heard', { timeout: 20000 });
      assert.match(await page.textContent('.heard'), /hey claude, what is this\?/);
      await page.waitForFunction(
        () => document.querySelectorAll('.exchange').length >= 2,
        null,
        { timeout: 20000 },
      );
      const questions = await page.$$eval('.exchange .question', (nodes) =>
        nodes.map((node) => node.textContent));
      assert.equal(questions[1], 'what is this?', 'the wake word is stripped from the prompt');
      await page.click('#listen');
    });

    await t.test('settings persist across a reload', async () => {
      await page.click('#settings-toggle');
      await page.waitForSelector('#wake:visible');
      await page.fill('#wake', 'computer');
      await page.dispatchEvent('#wake', 'change');
      await page.fill('#context', '0');
      await page.dispatchEvent('#context', 'change');
      await page.reload();
      await page.waitForSelector('#wake');
      assert.equal(await page.inputValue('#wake'), 'computer');
      assert.equal(await page.inputValue('#context'), '0');
      assert.match(await page.textContent('.empty'), /Computer, what does this error mean\?/,
        'the example uses the new wake word, capitalized as a name');
    });

    assert.deepEqual(errors, [], 'no uncaught page errors');
  });

  test('russian end-to-end', async (t) => {
    const { server, url } = await startServer({
      MICCLAUDE_LANG: 'ru',
      MICCLAUDE_FAKE_TRANSCRIPT: 'Клавдий, что это такое?',
    });
    const browser = await chromium.launch({
      args: [
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        `--use-file-for-fake-audio-capture=${writeFakeMicAudio()}`,
      ],
    });
    const context = await browser.newContext({ permissions: ['microphone'], locale: 'en-US' });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));

    t.after(async () => {
      await browser.close();
      server.kill();
    });

    await page.goto(url);

    await t.test('the page follows the language the server was started with', async () => {
      await page.waitForSelector('#facts dd', { state: 'attached' });
      assert.equal(await page.textContent('#listen'), 'Начать слушать');
      assert.equal(await page.textContent('#status-text'), 'Не слушаю');
      assert.equal(await page.getAttribute('html', 'lang'), 'ru');
      assert.equal(await page.inputValue('#wake'), 'клавдий');
      assert.match(await page.textContent('.empty'), /Клавдий, что означает эта ошибка\?/);
    });

    await t.test('speaking Russian reaches Claude with the name stripped', async () => {
      await page.click('#listen');
      await page.waitForSelector('.heard', { timeout: 20000 });
      assert.match(await page.textContent('.heard'), /Клавдий, что это такое\?/);
      await page.waitForSelector('.exchange .answer:not(.cursor)', { timeout: 20000 });
      assert.equal(await page.textContent('.exchange .question'), 'что это такое?');
      await page.click('#listen');
    });

    await t.test('the interface language can be overridden and is remembered', async () => {
      await page.click('#settings-toggle');
      await page.selectOption('#ui-language', 'en');
      assert.equal(await page.textContent('#listen'), 'Start listening');
      await page.reload();
      await page.waitForSelector('#facts dd', { state: 'attached' });
      assert.equal(await page.textContent('#listen'), 'Start listening');
      assert.equal(await page.getAttribute('html', 'lang'), 'en');
      assert.equal(await page.inputValue('#wake'), 'клавдий', 'the wake word is not translated');
    });

    assert.deepEqual(errors, [], 'no uncaught page errors');
  });
}
