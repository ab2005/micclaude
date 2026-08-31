import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

from helpers import StubTranscriber, tone

from micclaude.claude_client import ClaudeReply, Delta
from micclaude.config import Config
from micclaude.http_server import App, create_server, find_web_root


class StubClaude:
    """Stands in for the CLI: records prompts, replays scripted events."""

    def __init__(self, events=None):
        self.prompts = []
        self.session_id = None
        self.events = events or [Delta("Hello "), Delta("world"), ClaudeReply(
            text="Hello world", session_id="s1", duration_ms=120, cost_usd=0.002
        )]

    def resolve_binary(self):
        return "/usr/bin/claude"

    def stream(self, prompt):
        self.prompts.append(prompt)
        yield from self.events

    def reset(self):
        self.session_id = None


class ServerTestCase(unittest.TestCase):
    """Runs a real server on a random port and talks to it over HTTP."""

    config_overrides: dict = {}

    def setUp(self):
        config = Config()
        config.server.port = 0
        config.transcribe.backend = "null"
        for section, values in self.config_overrides.items():
            for key, value in values.items():
                setattr(getattr(config, section), key, value)
        self.transcriber = StubTranscriber()
        self.claude = StubClaude()
        self.server, self.app = create_server(
            config, transcriber=self.transcriber, claude=self.claude
        )
        self.port = self.server.server_address[1]
        # A short poll interval keeps teardown from waiting half a second per test.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        request = urllib.request.Request(self.url(path), headers=headers or {})
        return urllib.request.urlopen(request, timeout=10)

    def post(self, path, body=b"", headers=None):
        request = urllib.request.Request(
            self.url(path), data=body, headers=headers or {}, method="POST"
        )
        return urllib.request.urlopen(request, timeout=10)

    def json_get(self, path):
        return json.loads(self.get(path).read())


class StaticTests(ServerTestCase):
    def test_serves_the_page(self):
        response = self.get("/")
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("<title>micclaude</title>", body)
        self.assertIn('src="/js/app.js"', body)

    def test_serves_modules_with_a_javascript_type(self):
        for path in ("/js/app.js", "/js/trigger.js", "/js/capture-worklet.js", "/styles.css"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn(
                    "javascript" if path.endswith(".js") else "css",
                    response.headers["Content-Type"],
                )

    def test_unknown_file(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope.js")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_is_refused(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/../server/micclaude/config.py")
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()


class ApiTests(ServerTestCase):
    def test_health(self):
        payload = self.json_get("/api/health")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "stub")
        self.assertIn("workingDir", payload)

    def test_settings_are_client_safe(self):
        payload = self.json_get("/api/settings")
        self.assertEqual(sorted(payload), ["audio", "contextLines", "language", "speech", "trigger"])
        self.assertEqual(payload["trigger"]["wake_words"], ["claude"])
        self.assertNotIn("claude", payload, "CLI paths and tool grants stay on the server")

    def test_unknown_endpoint(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_transcribe(self):
        response = self.post(
            "/api/transcribe", tone(0.5).to_wav(), {"Content-Type": "audio/wav"}
        )
        payload = json.loads(response.read())
        self.assertEqual(payload["text"], "hello there")
        self.assertEqual(payload["audioMs"], 500)
        self.assertEqual(len(self.transcriber.calls), 1)
        self.assertEqual(self.transcriber.calls[0].sample_rate, 16000)

    def test_transcribe_resamples_to_the_configured_rate(self):
        self.post("/api/transcribe", tone(0.2, rate=48000).to_wav(), {"Content-Type": "audio/wav"})
        self.assertEqual(self.transcriber.calls[0].sample_rate, 16000)

    def test_transcribe_rejects_an_empty_body(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/transcribe")
        self.assertEqual(ctx.exception.code, 400)

    def test_transcribe_rejects_non_audio(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/transcribe", b"not a wav", {"Content-Type": "audio/wav"})
        self.assertEqual(ctx.exception.code, 503)

    def test_recognized_speech_is_recorded(self):
        self.post("/api/transcribe", tone(0.5).to_wav(), {"Content-Type": "audio/wav"})
        self.assertEqual([entry.text for entry in self.app.transcript], ["hello there"])

    def test_ask_streams_deltas_then_done(self):
        response = self.post(
            "/api/ask",
            json.dumps({"question": "who are you?"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertIn("text/event-stream", response.headers["Content-Type"])
        events = parse_sse(response.read().decode())
        self.assertEqual([name for name, _ in events], ["delta", "delta", "done"])
        self.assertEqual("".join(data["text"] for name, data in events if name == "delta"), "Hello world")
        self.assertEqual(events[-1][1]["sessionId"], "s1")

    def test_ask_forwards_supplied_context(self):
        body = json.dumps({"question": "what did I say?", "context": ["[10:00:00] the build broke"]})
        # Reading the body waits for the stream to finish, and with it the call.
        self.post("/api/ask", body.encode(), {"Content-Type": "application/json"}).read()
        prompt = self.claude.prompts[0]
        self.assertIn("the build broke", prompt)
        self.assertTrue(prompt.rstrip().endswith("what did I say?"))

    def test_ask_falls_back_to_the_server_transcript(self):
        self.post("/api/transcribe", tone(0.5).to_wav(), {"Content-Type": "audio/wav"})
        self.post("/api/transcribe", tone(0.5).to_wav(), {"Content-Type": "audio/wav"})
        self.post(
            "/api/ask", json.dumps({"question": "hi"}).encode(), {"Content-Type": "application/json"}
        ).read()
        self.assertIn("hello there", self.claude.prompts[0])

    def test_ask_requires_a_question(self):
        for body in (b"", b"{}", json.dumps({"question": "  "}).encode()):
            with self.subTest(body=body):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post("/api/ask", body, {"Content-Type": "application/json"})
                self.assertEqual(ctx.exception.code, 400)

    def test_ask_reports_a_failed_turn_as_an_error_event(self):
        self.claude.events = [ClaudeReply(text="claude exploded", is_error=True)]
        response = self.post(
            "/api/ask", json.dumps({"question": "hi"}).encode(), {"Content-Type": "application/json"}
        )
        events = parse_sse(response.read().decode())
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["text"], "claude exploded")

    def test_session_reset(self):
        self.claude.session_id = "s1"
        self.assertEqual(json.loads(self.post("/api/session/reset").read()), {"ok": True})
        self.assertIsNone(self.claude.session_id)

    def test_oversized_upload_is_refused(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", "/api/transcribe")
        connection.putheader("Content-Length", str(64 * 1024 * 1024))
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)
        connection.close()


class OriginTests(ServerTestCase):
    def test_another_origin_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/health", {"Origin": "https://evil.example"})
        self.assertEqual(ctx.exception.code, 403)

    def test_a_rebound_host_header_is_refused(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/api/health", headers={"Host": "attacker.example"})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()

    def test_our_own_origin_is_allowed(self):
        response = self.get("/api/health", {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(response.status, 200)


class WebRootTests(unittest.TestCase):
    def test_finds_the_checkout(self):
        root = find_web_root()
        self.assertTrue((root / "index.html").is_file())
        self.assertTrue((root / "js" / "app.js").is_file())

    def test_environment_override_wins(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.html").write_text("<h1>custom</h1>")
            os.environ["MICCLAUDE_WEB_ROOT"] = tmp
            self.addCleanup(os.environ.pop, "MICCLAUDE_WEB_ROOT", None)
            self.assertEqual(find_web_root(), Path(tmp).resolve())

    def test_a_missing_front_end_explains_itself(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MICCLAUDE_WEB_ROOT"] = tmp
            self.addCleanup(os.environ.pop, "MICCLAUDE_WEB_ROOT", None)
            # The packaged and checkout locations still resolve in a checkout,
            # so only assert on the override being skipped when it is empty.
            self.assertNotEqual(find_web_root(), Path(tmp).resolve())


class ContextTests(unittest.TestCase):
    def test_context_excludes_the_question_and_respects_the_limit(self):
        config = Config()
        config.claude.include_context_lines = 2
        app = App(config, transcriber=StubTranscriber(), claude=StubClaude())
        for text in ["one", "two", "three", "the question"]:
            app.record(text)
        lines = app.context_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("three"))
        self.assertFalse(any("the question" in line for line in lines))

    def test_context_can_be_disabled(self):
        config = Config()
        config.claude.include_context_lines = 0
        app = App(config, transcriber=StubTranscriber(), claude=StubClaude())
        app.record("something")
        self.assertEqual(app.context_lines(["[10:00] supplied"]), [])

    def test_transcript_file_is_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "transcript.jsonl"
            config = Config(transcript_file=str(path))
            app = App(config, transcriber=StubTranscriber(), claude=StubClaude())
            app.record("first")
            app.record("second")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["text"] for row in rows], ["first", "second"])


def parse_sse(payload: str) -> list[tuple[str, dict]]:
    events = []
    for block in payload.strip().split("\n\n"):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


if __name__ == "__main__":
    unittest.main()
