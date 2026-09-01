"""The recorder's segmenter, and that it agrees with the browser's."""

import array
import io
import json
import math
import shutil
import subprocess
import unittest
import wave
from pathlib import Path

import helpers  # noqa: F401  (puts the server package on sys.path)

from micclaude.capture import Segmenter, Utterance, rms
from micclaude.config import AudioConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFIG = AudioConfig(
    frame_ms=30,
    silence_ms=300,
    preroll_ms=90,
    min_utterance_ms=200,
    max_utterance_ms=5000,
    start_frames=2,
)


def frame(amplitude: float, config: AudioConfig = CONFIG) -> bytes:
    count = round(config.sample_rate * config.frame_ms / 1000)
    samples = array.array(
        "h",
        [
            int(amplitude * 32000 * math.sin(2 * math.pi * 220 * i / config.sample_rate))
            for i in range(count)
        ],
    )
    return samples.tobytes()


SILENCE = frame(0.0)
SPEECH = frame(0.5)


def repeat(value, count):
    return [value] * count


class SegmenterTests(unittest.TestCase):
    def test_two_phrases_separated_by_silence(self):
        frames = repeat(SILENCE, 5) + repeat(SPEECH, 10) + repeat(SILENCE, 12)
        frames += repeat(SPEECH, 10) + repeat(SILENCE, 12)
        utterances = list(Segmenter(CONFIG).feed(frames))
        self.assertEqual(len(utterances), 2)
        for utterance in utterances:
            self.assertGreater(utterance.duration_ms, 300)

    def test_audio_from_before_the_trigger_is_kept(self):
        frames = repeat(SILENCE, 5) + repeat(SPEECH, 10) + repeat(SILENCE, 12)
        utterance = next(iter(Segmenter(CONFIG).feed(frames)))
        self.assertEqual(round(utterance.duration_ms), 21 * 30)
        self.assertTrue(utterance.pcm.startswith(SILENCE))

    def test_a_short_blip_is_discarded(self):
        config = AudioConfig(**{**CONFIG.__dict__, "min_utterance_ms": 2000})
        frames = repeat(SPEECH, 4) + repeat(SILENCE, 12)
        self.assertEqual(list(Segmenter(config).feed(frames)), [])

    def test_a_long_monologue_is_cut_at_the_cap(self):
        config = AudioConfig(**{**CONFIG.__dict__, "max_utterance_ms": 600})
        utterances = list(Segmenter(config).feed(repeat(SPEECH, 60)))
        self.assertGreaterEqual(len(utterances), 2)
        self.assertLessEqual(utterances[0].duration_ms, 600)

    def test_silence_alone_produces_nothing(self):
        self.assertEqual(list(Segmenter(CONFIG).feed(repeat(SILENCE, 40))), [])

    def test_flush_closes_an_open_phrase_and_reset_drops_it(self):
        segmenter = Segmenter(CONFIG)
        for _ in range(10):
            segmenter.push(SPEECH)
        self.assertTrue(segmenter.is_speaking)
        self.assertIsNotNone(segmenter.flush())
        self.assertFalse(segmenter.is_speaking)

        for _ in range(10):
            segmenter.push(SPEECH)
        segmenter.reset()
        self.assertIsNone(segmenter.flush())

    def test_rms(self):
        self.assertEqual(rms(b""), 0.0)
        self.assertLess(rms(SILENCE), 0.001)
        self.assertGreater(rms(SPEECH), 0.3)
        self.assertGreaterEqual(rms(b"\x00\x01\x02"), 0.0, "an odd-length buffer is tolerated")


class UtteranceTests(unittest.TestCase):
    def test_wav_round_trip(self):
        utterance = Utterance(pcm=SPEECH, sample_rate=16000)
        with wave.open(io.BytesIO(utterance.to_wav())) as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.readframes(handle.getnframes()), SPEECH)

    def test_duration(self):
        self.assertAlmostEqual(Utterance(b"\x00" * 3200, 16000).duration_ms, 100.0)


@unittest.skipUnless(shutil.which("node"), "node is needed to run the browser's segmenter")
class ParityTests(unittest.TestCase):
    """The recorder and the browser must cut speech in the same places.

    Otherwise the same sentence reaches Claude differently depending on which
    one happened to hear it.
    """

    SCRIPT = """
    import { Segmenter } from './web/js/segmenter.js';
    const config = %s;
    const count = Math.round((config.sample_rate * config.frame_ms) / 1000);
    const tone = (amplitude) => {
      const out = new Float32Array(count);
      for (let i = 0; i < count; i += 1) {
        out[i] = (amplitude * 32000 * Math.sin((2 * Math.PI * 220 * i) / config.sample_rate)) / 32768;
      }
      return out;
    };
    const silence = tone(0), speech = tone(0.5);
    const pattern = %s;
    const frames = pattern.flatMap(([kind, n]) =>
      Array.from({ length: n }, () => (kind === 's' ? speech : silence)));
    const segmenter = new Segmenter(config);
    const durations = [];
    for (const frame of frames) {
      const utterance = segmenter.push(frame);
      if (utterance) durations.push(Math.round(utterance.durationMs));
    }
    const tail = segmenter.flush();
    if (tail) durations.push(Math.round(tail.durationMs));
    console.log(JSON.stringify(durations));
    """

    def browser_durations(self, pattern) -> list[int]:
        from dataclasses import asdict

        source = self.SCRIPT % (json.dumps(asdict(CONFIG)), json.dumps(pattern))
        result = subprocess.run(
            ["node", "--input-type=module", "-e", source],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def recorder_durations(self, pattern) -> list[int]:
        frames = []
        for kind, count in pattern:
            frames += repeat(SPEECH if kind == "s" else SILENCE, count)
        return [round(u.duration_ms) for u in Segmenter(CONFIG).feed(frames)]

    def test_the_same_speech_is_cut_the_same_way(self):
        patterns = [
            [("q", 5), ("s", 10), ("q", 12), ("s", 10), ("q", 12)],
            [("s", 3), ("q", 4), ("s", 3), ("q", 20)],
            [("q", 40)],
            [("s", 60)],
            [("q", 2), ("s", 1), ("q", 2), ("s", 25), ("q", 15)],
        ]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertEqual(self.recorder_durations(pattern), self.browser_durations(pattern))


if __name__ == "__main__":
    unittest.main()
