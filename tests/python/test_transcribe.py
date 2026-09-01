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


class ResampleTests(unittest.TestCase):
    """Downsampling must filter, not point-sample."""

    def tone(self, hz: int, rate: int, seconds: float = 0.2):
        import array
        import math

        count = int(rate * seconds)
        samples = array.array(
            "h", [int(20000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)]
        )
        return Utterance(pcm=samples.tobytes(), sample_rate=rate)

    def level(self, utterance: Utterance) -> float:
        return rms(utterance.pcm)

    def test_a_tone_above_the_new_nyquist_does_not_survive(self):
        """Point sampling would fold 12 kHz down into the speech band."""
        out = self.tone(12000, 48000).resample(16000)
        self.assertLess(self.level(out), 0.15, "aliasing is what makes Russian sibilants mush")

    def test_speech_band_content_comes_through(self):
        out = self.tone(300, 48000).resample(16000)
        self.assertGreater(self.level(out), 0.4)

    def test_the_duration_is_preserved(self):
        out = self.tone(300, 48000, seconds=0.5).resample(16000)
        self.assertAlmostEqual(out.duration_ms, 500, delta=5)

    def test_upsampling_still_interpolates(self):
        out = self.tone(300, 8000).resample(16000)
        self.assertAlmostEqual(out.duration_ms, 200, delta=5)
        self.assertGreater(self.level(out), 0.4)


class PromptEchoTests(unittest.TestCase):
    """Whisper hands the biasing prompt back when it cannot hear anything."""

    PROMPT = "Говорящий обращается к ассистенту по имени Клавдий."

    def test_the_prompt_returned_verbatim_is_dropped(self):
        from micclaude.transcribe import is_prompt_echo

        for text in (self.PROMPT, self.PROMPT.lower(), self.PROMPT.rstrip(".") + "  "):
            self.assertTrue(is_prompt_echo(text, self.PROMPT), text)

    def test_real_speech_is_not_dropped(self):
        from micclaude.transcribe import is_prompt_echo

        for text in ("Клавдий, что это?", "Говорящий обращается", "по имени Клавдий"):
            self.assertFalse(is_prompt_echo(text, self.PROMPT), text)

    def test_no_prompt_means_nothing_to_echo(self):
        from micclaude.transcribe import is_prompt_echo

        self.assertFalse(is_prompt_echo("что угодно", None))
        self.assertFalse(is_prompt_echo("", self.PROMPT))


class ContextPromptTests(unittest.TestCase):
    def transcriber(self, **kwargs):
        return FasterWhisperTranscriber(TranscribeConfig(**kwargs))

    def test_the_previous_phrase_is_offered_as_context(self):
        prompt = self.transcriber(initial_prompt="Имя: Клавдий.", context_words=3).prompt_for(
            "раз два три четыре пять"
        )
        self.assertEqual(prompt, "Имя: Клавдий. три четыре пять")

    def test_context_can_be_turned_off(self):
        prompt = self.transcriber(initial_prompt="Имя: Клавдий.", context_words=0).prompt_for("раз")
        self.assertEqual(prompt, "Имя: Клавдий.")

    def test_with_neither_there_is_no_prompt(self):
        self.assertIsNone(self.transcriber(initial_prompt=None, context_words=0).prompt_for(""))

    def test_an_echo_clears_the_context_rather_than_feeding_it_back(self):
        transcriber = self.transcriber(initial_prompt="Имя: Клавдий.")
        transcriber._previous = "что-то настоящее"
        self.assertEqual(transcriber._clean("Имя: Клавдий.", "Имя: Клавдий."), "")
        self.assertEqual(transcriber._previous, "", "a prompt that echoes is not context")

    def test_real_speech_becomes_the_next_context(self):
        transcriber = self.transcriber()
        self.assertEqual(transcriber._clean("тесты падают", None), "тесты падают")
        self.assertEqual(transcriber._previous, "тесты падают")


class DebugAudioTests(unittest.TestCase):
    def test_nothing_is_written_unless_asked(self):
        from micclaude.transcribe import save_debug_audio

        self.assertIsNone(save_debug_audio(tone(0.2), None))

    def test_the_utterance_is_saved_for_a_person_to_listen_to(self):
        import tempfile
        import wave
        from pathlib import Path

        from micclaude.transcribe import save_debug_audio

        with tempfile.TemporaryDirectory() as tmp:
            path = save_debug_audio(tone(0.3), str(Path(tmp) / "audio"))
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with wave.open(str(path)) as handle:
                self.assertEqual(handle.getframerate(), 16000)
                self.assertAlmostEqual(handle.getnframes() / 16000, 0.3, delta=0.01)


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

    def test_choosing_a_device_does_not_need_a_gpu(self):
        from micclaude.transcribe import _has_cuda

        self.assertIsInstance(_has_cuda(), bool)

    def test_loading_reaches_the_model_with_a_device_chosen(self):
        """Covers the path a machine with faster-whisper installed takes.

        Without the stub this never runs here -- load() raises on the import
        first -- which is how a NameError on the line after it survived.
        """
        import sys
        import types

        built = {}

        class StubModel:
            def __init__(self, model, device=None, compute_type=None):
                built.update(model=model, device=device, compute_type=compute_type)

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = StubModel
        sys.modules["faster_whisper"] = module
        self.addCleanup(sys.modules.pop, "faster_whisper", None)

        FasterWhisperTranscriber(TranscribeConfig(model="small", device="auto")).load()
        self.assertEqual(built["model"], "small")
        self.assertIn(built["device"], ("cpu", "cuda"))

    def test_an_explicit_device_is_honoured(self):
        import sys
        import types

        built = {}

        class StubModel:
            def __init__(self, model, device=None, compute_type=None):
                built["device"] = device

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = StubModel
        sys.modules["faster_whisper"] = module
        self.addCleanup(sys.modules.pop, "faster_whisper", None)

        FasterWhisperTranscriber(TranscribeConfig(device="cpu")).load()
        self.assertEqual(built["device"], "cpu")

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
