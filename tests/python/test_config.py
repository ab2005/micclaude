import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the server package on sys.path)

from micclaude.cli import apply_overrides, build_parser
from micclaude.config import Config, ConfigError, find_config_file, load_config


def write(tmp: str, body: str) -> Path:
    path = Path(tmp) / "micclaude.toml"
    path.write_text(body)
    return path


class LoadTests(unittest.TestCase):
    def test_defaults_without_a_file(self):
        config = load_config(None)
        self.assertEqual(config.server.port, 8765)
        self.assertEqual(config.trigger.wake_words, ["claude"])

    def test_overlays_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                tmp,
                """
                transcript_file = "notes.jsonl"

                [server]
                port = 9000

                [audio]
                silence_ms = 900

                [trigger]
                wake_words = ["computer", "jarvis"]

                [claude]
                timeout = 30
                allowed_tools = ["Read"]
                """,
            )
            config = load_config(path)
        self.assertEqual(config.transcript_file, "notes.jsonl")
        self.assertEqual(config.server.port, 9000)
        self.assertEqual(config.audio.silence_ms, 900)
        self.assertEqual(config.trigger.wake_words, ["computer", "jarvis"])
        self.assertEqual(config.claude.timeout, 30.0)
        self.assertEqual(config.claude.allowed_tools, ["Read"])

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "[audio]\nsampel_rate = 8000\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
        self.assertIn("audio.sampel_rate", str(ctx.exception))

    def test_wrong_types_are_rejected(self):
        cases = ['[trigger]\nwake_words = "claude"\n', "[server]\nopen_browser = 1\n", "audio = 3\n"]
        for body in cases:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfigError):
                    load_config(write(tmp, body))

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaises(ConfigError):
            find_config_file("/nonexistent/micclaude.toml")

    def test_client_settings_leave_the_cli_configuration_behind(self):
        config = Config()
        config.claude.allowed_tools = ["Read"]
        config.claude.working_dir = "/srv/secret"
        settings = config.client_settings()
        self.assertEqual(sorted(settings), ["audio", "contextLines", "speech", "trigger"])
        self.assertNotIn("/srv/secret", str(settings))


class OverrideTests(unittest.TestCase):
    def parse(self, *argv):
        return apply_overrides(Config(), build_parser().parse_args(list(argv)))

    def test_server_flags(self):
        config = self.parse("--host", "0.0.0.0", "--port", "9100", "--no-browser")
        self.assertEqual(config.server.host, "0.0.0.0")
        self.assertEqual(config.server.port, 9100)
        self.assertFalse(config.server.open_browser)

    def test_transcription_flags(self):
        config = self.parse("--backend", "openai", "--model", "small.en", "--language", "auto")
        self.assertEqual(config.transcribe.backend, "openai")
        self.assertEqual(config.transcribe.model, "small.en")
        self.assertIsNone(config.transcribe.language)

    def test_claude_flags(self):
        config = self.parse(
            "--claude-model", "claude-sonnet-5",
            "--claude-dir", "/srv/project",
            "--permission-mode", "acceptEdits",
            "--allow-tool", "Read",
            "--allow-tool", "Grep",
        )
        self.assertEqual(config.claude.model, "claude-sonnet-5")
        self.assertEqual(config.claude.working_dir, "/srv/project")
        self.assertEqual(config.claude.permission_mode, "acceptEdits")
        self.assertEqual(config.claude.allowed_tools, ["Read", "Grep"])

    def test_context_and_session_flags(self):
        config = self.parse("--no-context", "--fresh-session")
        self.assertEqual(config.claude.include_context_lines, 0)
        self.assertFalse(config.claude.continue_session)

    def test_wake_words_replace_the_default(self):
        self.assertEqual(self.parse("--wake", "computer").trigger.wake_words, ["computer"])

    def test_defaults_are_untouched_without_flags(self):
        self.assertEqual(self.parse(), Config())


class MainTests(unittest.TestCase):
    def test_print_config(self):
        import io
        import json
        from contextlib import redirect_stdout

        from micclaude.cli import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--print-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["server"]["port"], 8765)

    def test_bad_config_path_exits_two(self):
        import io
        from contextlib import redirect_stderr

        from micclaude.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--config", "/nonexistent/micclaude.toml"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
