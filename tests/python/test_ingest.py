"""Text recognized elsewhere: POST /api/utterance, GET /api/events, catch-up."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import StubTranscriber
from test_http_server import ServerTestCase, StubClaude, parse_sse

from micclaude.broadcast import Broadcaster


class BroadcasterTests(unittest.TestCase):
    def test_an_event_reaches_every_subscriber(self):
        broadcaster = Broadcaster()
        first, second = broadcaster.subscribe(), broadcaster.subscribe()
        self.assertEqual(broadcaster.publish("utterance", {"text": "hi"}), 2)
        for subscription in (first, second):
            self.assertEqual(next(subscription.events()), ("utterance", {"text": "hi"}))

    def test_a_slow_subscriber_loses_its_oldest_events(self):
        broadcaster = Broadcaster(queue_limit=2)
        subscription = broadcaster.subscribe()
        for index in range(4):
            broadcaster.publish("utterance", {"n": index})
        events = subscription.events()
        self.assertEqual([next(events)[1]["n"] for _ in range(2)], [2, 3], "newest survive")

    def test_unsubscribing_stops_delivery(self):
        broadcaster = Broadcaster()
        subscription = broadcaster.subscribe()
        subscription.close()
        self.assertEqual(broadcaster.publish("utterance", {}), 0)
        self.assertEqual(broadcaster.subscriber_count, 0)

    def test_closing_ends_every_stream(self):
        broadcaster = Broadcaster()
        subscription = broadcaster.subscribe()
        broadcaster.close()
        self.assertEqual(list(subscription.events()), [])

    def test_a_quiet_stream_yields_keepalives(self):
        subscription = Broadcaster().subscribe()
        self.assertIsNone(next(subscription.events(keepalive=0.01)))


class UtteranceTests(ServerTestCase):
    def post_utterance(self, payload, headers=None):
        return self.post(
            "/api/utterance",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json", **(headers or {})},
        )

    def test_posted_text_is_recorded(self):
        response = self.post_utterance({"text": "интеграционные тесты падают"})
        entry = json.loads(response.read())
        self.assertEqual(response.status, 201)
        self.assertEqual(entry["text"], "интеграционные тесты падают")
        self.assertEqual(entry["source"], "recorder")
        self.assertEqual([e.text for e in self.app.transcript], ["интеграционные тесты падают"])

    def test_posted_text_reaches_the_transcript_file(self):
        self.post_utterance({"text": "на диск тоже"})
        from pathlib import Path

        files = sorted(Path(self.transcripts.name).rglob("*.jsonl"))
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["text"], "на диск тоже")

    def test_the_recorder_may_supply_its_own_timestamp(self):
        entry = json.loads(self.post_utterance({"text": "hi", "time": 1000.5}).read())
        self.assertEqual(entry["time"], 1000.5)

    def test_the_source_can_be_named(self):
        entry = json.loads(self.post_utterance({"text": "hi", "source": "zoom"}).read())
        self.assertEqual(entry["source"], "zoom")

    def test_the_poster_is_remembered_so_a_page_can_ignore_its_echo(self):
        entry = json.loads(self.post_utterance({"text": "hi"}, {"X-Client-Id": "page-1"}).read())
        self.assertEqual(entry["client"], "page-1")

    def test_entries_are_numbered_in_order(self):
        ids = [json.loads(self.post_utterance({"text": f"{n}"}).read())["id"] for n in range(3)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 3)

    def test_empty_and_malformed_bodies_are_refused(self):
        for body in (b"", b"{}", b'{"text": "   "}', b"not json", b'["a"]'):
            with self.subTest(body=body):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post("/api/utterance", body, {"Content-Type": "application/json"})
                self.assertEqual(ctx.exception.code, 400)


class CatchUpTests(ServerTestCase):
    def test_a_page_that_just_opened_can_read_the_recent_transcript(self):
        for text in ("первая", "вторая"):
            self.post(
                "/api/utterance",
                json.dumps({"text": text}).encode(),
                {"Content-Type": "application/json"},
            )
        entries = self.json_get("/api/transcript")["entries"]
        self.assertEqual([entry["text"] for entry in entries], ["первая", "вторая"])


class EventStreamTests(ServerTestCase):
    def read_events(self, count: int, then) -> list:
        """Open the stream, run `then`, and collect `count` events."""
        events: list = []
        error: list = []
        opened: list = []

        def reader():
            try:
                response = urllib.request.urlopen(self.url("/api/events"), timeout=10)
                opened.append(response)
                # Bytes, not text: a multi-byte character can straddle a read.
                buffer = b""
                while len(events) < count:
                    chunk = response.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        events.extend(parse_sse(block.decode("utf-8") + "\n\n"))
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                error.append(exc)
            finally:
                for response in opened:
                    response.close()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while self.app.events.subscriber_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.app.events.subscriber_count, 1, "the stream did not open")
        then()
        thread.join(timeout=10)
        self.assertEqual(error, [])
        return events

    def test_posted_text_is_pushed_to_an_open_page(self):
        events = self.read_events(1, lambda: self.post_utterance("Клавдий, что это?"))
        self.assertEqual(events[0][0], "utterance")
        self.assertEqual(events[0][1]["text"], "Клавдий, что это?")

    def test_transcribed_audio_is_pushed_too(self):
        from helpers import tone

        def transcribe():
            self.post("/api/transcribe", tone(0.5).to_wav(), {"Content-Type": "audio/wav"})

        events = self.read_events(1, transcribe)
        self.assertEqual(events[0][1]["source"], "browser")
        self.assertEqual(events[0][1]["text"], "hello there")

    def test_a_page_that_went_away_is_forgotten(self):
        """A dead client is discovered on the next write, not before.

        Nothing polls the socket, so the subscription survives until the
        server tries to push something to it -- which is what happens here.
        """
        self.read_events(1, lambda: self.post_utterance("hi"))
        self.post_utterance("this one fails to reach the closed page")
        deadline = time.monotonic() + 5
        while self.app.events.subscriber_count and time.monotonic() < deadline:
            time.sleep(0.02)
            self.post_utterance("and again")
        self.assertEqual(self.app.events.subscriber_count, 0)

    def post_utterance(self, text: str):
        return self.post(
            "/api/utterance",
            json.dumps({"text": text}).encode(),
            {"Content-Type": "application/json"},
        )


if __name__ == "__main__":
    unittest.main()
