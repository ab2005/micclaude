"""The whisper.cpp backend: model lookup, the server it runs, what it parses.

The server is faked, so everything except whisper.cpp's own inference is
exercised for real: the child process, readiness polling, the multipart upload,
the JSON reply, and shutting it down.
"""

import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the server package on sys.path)

from micclaude.config import TranscribeConfig
from micclaude.transcribe import (
    TranscriptionError,
    WhisperCppTranscriber,
    build_transcriber,
)

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SelectionTests(unittest.TestCase):
    def test_the_backend_answers_to_several_names(self):
        for name in ("whisper.cpp", "whispercpp", "whisper-cpp", "metal"):
            with self.subTest(name=name):
                built = build_transcriber(TranscribeConfig(backend=name))
                self.assertIsInstance(built, WhisperCppTranscriber)
                self.assertEqual(built.name, "whisper.cpp")


class ModelPathTests(unittest.TestCase):
    def path(self, model: str, **kwargs) -> Path:
        return WhisperCppTranscriber(TranscribeConfig(model=model, **kwargs)).model_path()

    def test_a_bare_name_is_looked_up_in_the_model_directory(self):
        self.assertEqual(
            self.path("small", model_dir="/models"), Path("/models/ggml-small.bin")
        )

    def test_a_name_that_already_says_ggml_is_not_doubled(self):
        self.assertEqual(
            self.path("ggml-small", model_dir="/models"), Path("/models/ggml-small.bin")
        )

    def test_a_path_is_taken_as_given(self):
        self.assertEqual(self.path("/opt/models/m.bin"), Path("/opt/models/m.bin"))

    def test_a_tilde_is_expanded(self):
        self.assertTrue(str(self.path("~/m/ggml-tiny.bin")).startswith(str(Path.home())))


class ServerArgvTests(unittest.TestCase):
    def argv(self, **kwargs):
        config = TranscribeConfig(**kwargs)
        return WhisperCppTranscriber(config).server_argv(Path("/m/ggml-small.bin"), 8181)

    def test_the_model_and_port_are_passed(self):
        self.assertEqual(
            self.argv(language=None),
            ["whisper-server", "-m", "/m/ggml-small.bin", "--port", "8181"],
        )

    def test_the_language_is_passed_when_set(self):
        self.assertEqual(self.argv(language="ru")[-2:], ["-l", "ru"])

    def test_extra_flags_come_last(self):
        self.assertEqual(self.argv(server_args=["-t", "4"])[-2:], ["-t", "4"])


class RunningServerTests(unittest.TestCase):
    """Against the fake server, started the way the real one would be."""

    def transcriber(self, **kwargs) -> WhisperCppTranscriber:
        port = free_port()
        model = Path(self.tmp.name) / "ggml-small.bin"
        model.write_bytes(b"not really a model")
        config = TranscribeConfig(
            backend="whisper.cpp",
            model=str(model),
            server_url=f"http://127.0.0.1:{port}",
            server_binary=str(self.binary),
            **{"startup_timeout": 20.0, **kwargs},
        )
        transcriber = WhisperCppTranscriber(config)
        self.addCleanup(transcriber.stop)
        return transcriber

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A wrapper so the "binary" is one executable file, as whisper-server is.
        self.binary = Path(self.tmp.name) / "whisper-server"
        self.binary.write_text(
            f'#!/bin/sh\nexec "{os.sys.executable}" "{HERE / "fake_whisper_server.py"}" "$@"\n'
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)

    def test_it_starts_the_server_and_transcribes(self):
        from helpers import tone

        transcriber = self.transcriber()
        self.assertEqual(transcriber.transcribe(tone(0.5)), "hello there")

    def test_the_audio_actually_arrives(self):
        import json
        import urllib.request

        from helpers import tone

        transcriber = self.transcriber()
        transcriber.load()
        utterance = tone(0.5)
        transcriber.transcribe(utterance)
        # The fake echoes the upload size; a WAV of this length must have made it.
        body, content_type = self._multipart_probe(utterance)
        request = urllib.request.Request(
            transcriber.config.server_url + "/inference", data=body,
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        self.assertGreater(payload["bytes"], len(utterance.pcm))

    def _multipart_probe(self, utterance):
        from micclaude.transcribe import _multipart

        return _multipart({"response_format": "json"}, filename="s.wav", file_bytes=utterance.to_wav())

    def test_a_phantom_phrase_is_dropped(self):
        from helpers import tone

        os.environ["FAKE_WHISPER_TEXT"] = "Продолжение следует..."
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_TEXT", None)
        self.assertEqual(self.transcriber().transcribe(tone(0.5)), "")

    def test_stopping_it_twice_is_harmless(self):
        transcriber = self.transcriber()
        transcriber.load()
        transcriber.stop()
        transcriber.stop()

    def test_a_server_error_is_reported_not_swallowed(self):
        from helpers import tone

        os.environ["FAKE_WHISPER_HTTP_ERROR"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_HTTP_ERROR", None)
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcriber().transcribe(tone(0.5))
        self.assertIn("500", str(ctx.exception))

    def test_a_reply_that_is_not_json_is_reported(self):
        from helpers import tone

        os.environ["FAKE_WHISPER_GARBAGE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_GARBAGE", None)
        with self.assertRaises(TranscriptionError):
            self.transcriber().transcribe(tone(0.5))

    def test_a_server_that_dies_at_startup_says_why(self):
        os.environ["FAKE_WHISPER_FAIL"] = "could not load model"
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_FAIL", None)
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcriber().load()
        self.assertIn("could not load model", str(ctx.exception))

    def test_a_slow_server_is_waited_for(self):
        os.environ["FAKE_WHISPER_STARTUP_DELAY"] = "1.5"
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_STARTUP_DELAY", None)
        transcriber = self.transcriber()
        transcriber.load()  # must not raise

    def test_a_server_that_never_comes_up_times_out(self):
        os.environ["FAKE_WHISPER_STARTUP_DELAY"] = "30"
        self.addCleanup(os.environ.pop, "FAKE_WHISPER_STARTUP_DELAY", None)
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcriber(startup_timeout=1.0).load()
        self.assertIn("did not come up", str(ctx.exception))


class NotStartingTests(unittest.TestCase):
    def test_a_missing_binary_explains_how_to_get_one(self):
        config = TranscribeConfig(
            backend="whisper.cpp",
            server_binary="definitely-not-installed-xyz",
            server_url=f"http://127.0.0.1:{free_port()}",
        )
        with self.assertRaises(TranscriptionError) as ctx:
            WhisperCppTranscriber(config).load()
        self.assertIn("brew install whisper-cpp", str(ctx.exception))

    def test_a_missing_model_explains_where_to_get_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "whisper-server"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
            config = TranscribeConfig(
                backend="whisper.cpp",
                model="small",
                model_dir=tmp,
                server_binary=str(binary),
                server_url=f"http://127.0.0.1:{free_port()}",
            )
            with self.assertRaises(TranscriptionError) as ctx:
                WhisperCppTranscriber(config).load()
        message = str(ctx.exception)
        self.assertIn("ggml-small.bin", message)
        self.assertIn("huggingface.co", message, "the error says where to get one")

    def test_without_autostart_it_refuses_to_start_anything(self):
        config = TranscribeConfig(
            backend="whisper.cpp",
            autostart=False,
            server_url=f"http://127.0.0.1:{free_port()}",
        )
        with self.assertRaises(TranscriptionError) as ctx:
            WhisperCppTranscriber(config).load()
        self.assertIn("nothing is listening", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
