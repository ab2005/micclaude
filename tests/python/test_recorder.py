"""Delivering recognized text to the server, including when it is down."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from test_http_server import ServerTestCase

from micclaude.config import Config
from micclaude.recorder import Delivery, build_parser


class DeliveryTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.spool_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.spool_dir.cleanup)
        self.spool = Path(self.spool_dir.name) / "pending.jsonl"

    def delivery(self, server=None, **kwargs):
        return Delivery(server or f"http://127.0.0.1:{self.port}", spool=self.spool, **kwargs)

    def test_a_line_reaches_the_server(self):
        delivery = self.delivery()
        self.assertTrue(delivery.send("интеграционные тесты падают"))
        self.assertEqual([e.text for e in self.app.transcript], ["интеграционные тесты падают"])
        self.assertEqual(delivery.pending, [])
        self.assertEqual(delivery.delivered, 1)

    def test_the_source_name_is_carried(self):
        self.delivery(source="zoom").send("hi")
        self.assertEqual(self.app.transcript[0].source, "zoom")

    def test_the_recorder_supplies_the_time_it_heard_it(self):
        self.delivery().send("hi", timestamp=1000.5)
        self.assertEqual(self.app.transcript[0].timestamp, 1000.5)

    def test_an_unreachable_server_holds_the_line(self):
        delivery = self.delivery("http://127.0.0.1:9")  # discard port: nothing listens
        self.assertFalse(delivery.send("не потеряй меня"))
        self.assertEqual([e["text"] for e in delivery.pending], ["не потеряй меня"])

    def test_held_lines_are_delivered_in_order_when_it_comes_back(self):
        offline = self.delivery("http://127.0.0.1:9")
        for text in ("первая", "вторая", "третья"):
            offline.send(text)
        self.assertEqual(len(offline.pending), 3)

        # Same spool file, a server that answers: the backlog goes out first.
        offline.save_spool()
        online = self.delivery()
        self.assertTrue(online.flush())
        self.assertEqual([e.text for e in self.app.transcript], ["первая", "вторая", "третья"])

    def test_a_refused_line_is_dropped_rather_than_wedging_the_queue(self):
        delivery = self.delivery()
        delivery.pending.append({"text": "", "source": "recorder"})  # the server will 400 this
        self.assertTrue(delivery.send("и эта должна дойти"))
        self.assertEqual([e.text for e in self.app.transcript], ["и эта должна дойти"])
        self.assertEqual(delivery.pending, [])

    def test_the_spool_is_written_and_removed(self):
        offline = self.delivery("http://127.0.0.1:9")
        offline.send("ждёт своего часа")
        offline.close()
        self.assertEqual(
            [json.loads(line)["text"] for line in self.spool.read_text(encoding="utf-8").splitlines()],
            ["ждёт своего часа"],
        )

        online = self.delivery()
        self.assertEqual([e["text"] for e in online.pending], ["ждёт своего часа"], "picked up")
        online.close()
        self.assertFalse(self.spool.exists(), "nothing left to keep")

    def test_a_corrupt_spool_line_is_skipped(self):
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        self.spool.write_text('not json\n{"text": "цела"}\n{"no": "text"}\n', encoding="utf-8")
        self.assertEqual([e["text"] for e in self.delivery().pending], ["цела"])

    def test_spooling_can_be_turned_off(self):
        delivery = Delivery(f"http://127.0.0.1:{self.port}", spool=None)
        delivery.send("hi")
        delivery.close()
        self.assertIsNone(delivery.spool)


class ParserTests(unittest.TestCase):
    def parse(self, *argv):
        return build_parser().parse_args(list(argv))

    def test_defaults(self):
        args = self.parse()
        self.assertEqual(args.server, "http://127.0.0.1:8765")
        self.assertEqual(args.source, "recorder")
        self.assertFalse(args.no_spool)

    def test_flags(self):
        args = self.parse("--lang", "ru", "--server", "http://air.local:8765", "--source", "room")
        self.assertEqual(args.lang, "ru")
        self.assertEqual(args.server, "http://air.local:8765")
        self.assertEqual(args.source, "room")

    def test_an_unknown_language_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parse("--lang", "kl")


class ConfigurationTests(unittest.TestCase):
    def test_the_language_preset_reaches_the_recorder(self):
        from micclaude.config import apply_language

        config = apply_language(Config(), "ru")
        self.assertEqual(config.transcribe.model, "small")
        self.assertEqual(config.audio.sample_rate, 16000, "the recorder records what whisper wants")


if __name__ == "__main__":
    unittest.main()
