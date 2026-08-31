import array
import io
import math
import unittest
import wave

from helpers import tone

from micclaude.config import TranscribeConfig
from micclaude.transcribe import (
    FasterWhisperTranscriber,
    NullTranscriber,
    OpenAITranscriber,
    TranscriptionError,
    Utterance,
    _multipart,
    build_transcriber,
    decode_wav,
    rms,
)


class WavTests(unittest.TestCase):
    def test_round_trip(self):
        utterance = tone(0.1)
        decoded = decode_wav(utterance.to_wav())
        self.assertEqual(decoded.sample_rate, 16000)
        self.assertEqual(decoded.pcm, utterance.pcm)

    def test_duration(self):
        self.assertAlmostEqual(Utterance(b"\x00" * 3200, 16000).duration_ms, 100.0)

    def test_resamples_to_target(self):
        decoded = decode_wav(tone(0.1, rate=48000).to_wav(), target_rate=16000)
        self.assertEqual(decoded.sample_rate, 16000)
        self.assertAlmostEqual(decoded.duration_ms, 100.0, delta=1.0)

    def test_stereo_is_mixed_to_mono(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(array.array("h", [1000, 3000] * 100).tobytes())
        decoded = decode_wav(buffer.getvalue())
        samples = array.array("h")
        samples.frombytes(decoded.pcm)
        self.assertEqual(len(samples), 100)
        self.assertEqual(set(samples), {2000})

    def test_rejects_garbage(self):
        with self.assertRaises(TranscriptionError):
            decode_wav(b"this is not a wav file")

    def test_rejects_8_bit_audio(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(16000)
            handle.writeframes(b"\x80" * 100)
        with self.assertRaises(TranscriptionError) as ctx:
            decode_wav(buffer.getvalue())
        self.assertIn("16-bit", str(ctx.exception))

    def test_rms(self):
        self.assertEqual(rms(b""), 0.0)
        self.assertGreater(rms(tone(0.05).pcm), 0.2)
        self.assertAlmostEqual(rms(b"\x00" * 100), 0.0)


class BackendTests(unittest.TestCase):
    def test_factory(self):
        self.assertIsInstance(build_transcriber(TranscribeConfig()), FasterWhisperTranscriber)
        self.assertIsInstance(
            build_transcriber(TranscribeConfig(backend="openai")), OpenAITranscriber
        )
        self.assertIsInstance(build_transcriber(TranscribeConfig(backend="null")), NullTranscriber)

    def test_unknown_backend(self):
        with self.assertRaises(TranscriptionError):
            build_transcriber(TranscribeConfig(backend="wishful"))

    def test_openai_backend_requires_a_key(self):
        config = TranscribeConfig(backend="openai", api_key_env="MICCLAUDE_TEST_MISSING_KEY")
        with self.assertRaises(TranscriptionError) as ctx:
            OpenAITranscriber(config).transcribe(tone(0.05))
        self.assertIn("MICCLAUDE_TEST_MISSING_KEY", str(ctx.exception))

    def test_missing_faster_whisper_explains_itself(self):
        transcriber = FasterWhisperTranscriber(TranscribeConfig(model="tiny.en"))
        try:
            import faster_whisper  # type: ignore # noqa: F401
        except ImportError:
            with self.assertRaises(TranscriptionError) as ctx:
                transcriber.load()
            self.assertIn("pip install faster-whisper", str(ctx.exception))
        else:  # pragma: no cover - only when the optional dep is installed
            self.skipTest("faster-whisper is installed")

    def test_multipart_encodes_the_file(self):
        body, content_type = _multipart({"model": "whisper-1"}, filename="a.wav", file_bytes=b"RIFF")
        self.assertIn("multipart/form-data; boundary=", content_type)
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.endswith(f"\r\n--{boundary}--\r\n".encode()))
        self.assertIn(b'name="model"', body)
        self.assertIn(b'filename="a.wav"', body)
        self.assertIn(b"RIFF", body)


if __name__ == "__main__":
    unittest.main()
