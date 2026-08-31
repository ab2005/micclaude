import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from helpers import STREAMING_CLAUDE, prepend_path, write_fake_claude

from micclaude.claude_client import (
    ClaudeClient,
    ClaudeNotFound,
    ClaudeReply,
    Delta,
    format_prompt,
    parse_result,
    text_delta,
)
from micclaude.config import ClaudeConfig


class ParseTests(unittest.TestCase):
    def test_single_result_object(self):
        reply = parse_result(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": " hello ",
                    "session_id": "abc",
                    "duration_ms": 1200,
                    "total_cost_usd": 0.004,
                }
            )
        )
        self.assertEqual(reply.text, "hello")
        self.assertEqual(reply.session_id, "abc")
        self.assertEqual(reply.duration_ms, 1200)
        self.assertFalse(reply.is_error)

    def test_list_of_events_uses_the_last_result(self):
        payload = [
            {"type": "system"},
            {"type": "result", "subtype": "success", "result": "first"},
            {"type": "result", "subtype": "success", "result": "second"},
        ]
        self.assertEqual(parse_result(json.dumps(payload)).text, "second")

    def test_json_lines(self):
        stdout = '{"type":"system"}\n{"type":"result","subtype":"success","result":"streamed"}\n'
        self.assertEqual(parse_result(stdout).text, "streamed")

    def test_error_subtype_is_flagged(self):
        reply = parse_result(json.dumps({"type": "result", "subtype": "error_during_execution"}))
        self.assertTrue(reply.is_error)

    def test_non_json(self):
        self.assertIsNone(parse_result("command not found"))
        self.assertIsNone(parse_result(""))

    def test_text_delta_extraction(self):
        event = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        }
        self.assertEqual(text_delta(event), "hi")
        self.assertEqual(text_delta({"type": "assistant"}), "")
        self.assertEqual(
            text_delta({"type": "stream_event", "event": {"type": "message_stop"}}), ""
        )
        thinking = {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta"}},
        }
        self.assertEqual(text_delta(thinking), "")


class ArgvTests(unittest.TestCase):
    def test_defaults(self):
        argv = ClaudeClient(ClaudeConfig(append_system_prompt="")).build_argv("hi")
        self.assertEqual(argv, ["claude", "-p", "hi", "--output-format", "json"])

    def test_streaming_flags(self):
        argv = ClaudeClient(ClaudeConfig(append_system_prompt="")).build_argv("hi", stream=True)
        self.assertEqual(argv[3:], ["--output-format", "stream-json", "--include-partial-messages", "--verbose"])

    def test_options_are_passed_through(self):
        config = ClaudeConfig(
            append_system_prompt="",
            model="claude-sonnet-5",
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Grep"],
            disallowed_tools=["Bash"],
            extra_args=["--add-dir", "/tmp"],
        )
        argv = ClaudeClient(config).build_argv("hi")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--allowedTools") + 1 : argv.index("--allowedTools") + 3], ["Read", "Grep"])
        self.assertEqual(argv[-2:], ["--add-dir", "/tmp"])

    def test_resume_only_after_a_session_exists(self):
        client = ClaudeClient(ClaudeConfig(append_system_prompt=""))
        self.assertNotIn("--resume", client.build_argv("hi"))
        client.session_id = "sess-1"
        self.assertEqual(client.build_argv("hi")[-2:], ["--resume", "sess-1"])

    def test_continue_session_disabled(self):
        client = ClaudeClient(ClaudeConfig(continue_session=False))
        client.session_id = "sess-1"
        self.assertNotIn("--resume", client.build_argv("hi"))


class SubprocessTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name)
        original = prepend_path(self.bin)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", original))

    def test_missing_binary(self):
        client = ClaudeClient(ClaudeConfig(binary="definitely-not-installed-xyz"))
        with self.assertRaises(ClaudeNotFound):
            client.ask("hi")
        with self.assertRaises(ClaudeNotFound):
            list(client.stream("hi"))

    def test_ask_returns_text_and_captures_the_session(self):
        write_fake_claude(
            self.bin,
            'echo \'{"type":"result","subtype":"success","result":"forty two","session_id":"s9"}\'\n',
        )
        client = ClaudeClient(ClaudeConfig())
        self.assertEqual(client.ask("6 times 7").text, "forty two")
        self.assertEqual(client.session_id, "s9")

    def test_ask_passes_the_prompt(self):
        write_fake_claude(
            self.bin, 'printf \'{"type":"result","subtype":"success","result":"%s"}\' "$2"\n'
        )
        client = ClaudeClient(ClaudeConfig(append_system_prompt=""))
        self.assertEqual(client.ask("echo me").text, "echo me")

    def test_ask_reports_failures(self):
        write_fake_claude(self.bin, 'echo "kaboom" >&2\nexit 1\n')
        reply = ClaudeClient(ClaudeConfig()).ask("hi")
        self.assertTrue(reply.is_error)
        self.assertIn("kaboom", reply.text)

    def test_ask_timeout(self):
        write_fake_claude(self.bin, "sleep 5\n")
        reply = ClaudeClient(ClaudeConfig(timeout=0.3)).ask("hi")
        self.assertTrue(reply.is_error)
        self.assertIn("did not answer", reply.text)

    def test_stream_yields_deltas_then_a_result(self):
        write_fake_claude(self.bin, STREAMING_CLAUDE)
        client = ClaudeClient(ClaudeConfig())
        items = list(client.stream("hi"))
        self.assertEqual([i.text for i in items if isinstance(i, Delta)], ["Hello ", "world"])
        final = items[-1]
        self.assertIsInstance(final, ClaudeReply)
        self.assertEqual(final.text, "Hello world")
        self.assertEqual(final.cost_usd, 0.002)
        self.assertEqual(client.session_id, "sess-1")

    def test_stream_reports_a_crash(self):
        write_fake_claude(self.bin, 'echo "boom" >&2\nexit 2\n')
        items = list(ClaudeClient(ClaudeConfig()).stream("hi"))
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].is_error)
        self.assertIn("boom", items[0].text)

    def test_stream_retries_once_when_the_session_is_gone(self):
        write_fake_claude(
            self.bin,
            'case "$*" in\n'
            '  *--resume*) echo "No conversation found with session ID: gone" >&2; exit 1;;\n'
            f"  *) {STREAMING_CLAUDE};;\n"
            "esac\n",
        )
        client = ClaudeClient(ClaudeConfig())
        client.session_id = "gone"
        items = list(client.stream("hi"))
        self.assertEqual(items[-1].text, "Hello world")
        self.assertEqual(client.session_id, "sess-1")

    def test_reset_forgets_the_session(self):
        client = ClaudeClient(ClaudeConfig())
        client.session_id = "s"
        client.reset()
        self.assertIsNone(client.session_id)


class FormatPromptTests(unittest.TestCase):
    def test_without_context(self):
        self.assertEqual(format_prompt("  what is this?  "), "what is this?")

    def test_with_context(self):
        prompt = format_prompt("summarize that", ["[10:00:00] a", "[10:00:05] b"])
        self.assertIn("[10:00:00] a", prompt)
        self.assertTrue(prompt.rstrip().endswith("summarize that"))


if __name__ == "__main__":
    unittest.main()
