import calendar
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the server package on sys.path)

from micclaude.transcript import TranscriptWriter


def at(year, month, day, hour, minute=0) -> float:
    """A local-time moment as epoch seconds."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "transcripts"
        self.writer = TranscriptWriter(self.root)

    def relative(self, path):
        return str(path.relative_to(self.root))

    def test_a_file_per_hour_inside_a_directory_per_day(self):
        path = self.writer.write(at(2026, 8, 31, 14, 5), "первая фраза")
        self.assertEqual(self.relative(path), os.path.join("2026-08-31", "14.jsonl"))

    def test_the_same_hour_appends(self):
        first = self.writer.write(at(2026, 8, 31, 14, 5), "one")
        second = self.writer.write(at(2026, 8, 31, 14, 59), "two")
        self.assertEqual(first, second)
        self.assertEqual([json.loads(line)["text"] for line in first.read_text().splitlines()], ["one", "two"])

    def test_the_next_hour_starts_a_new_file(self):
        self.writer.write(at(2026, 8, 31, 14, 59), "before")
        after = self.writer.write(at(2026, 8, 31, 15, 0), "after")
        self.assertEqual(self.relative(after), os.path.join("2026-08-31", "15.jsonl"))
        self.assertEqual(sorted(p.name for p in (self.root / "2026-08-31").iterdir()),
                         ["14.jsonl", "15.jsonl"])

    def test_midnight_starts_a_new_day(self):
        self.writer.write(at(2026, 8, 31, 23, 59), "before")
        after = self.writer.write(at(2026, 9, 1, 0, 1), "after")
        self.assertEqual(self.relative(after), os.path.join("2026-09-01", "00.jsonl"))

    def test_records_keep_the_timestamp_and_the_text(self):
        when = at(2026, 8, 31, 14, 5)
        path = self.writer.write(when, "интеграционные тесты падают")
        record = json.loads(path.read_text())
        self.assertEqual(record["time"], when)
        self.assertEqual(record["text"], "интеграционные тесты падают")

    def test_cyrillic_is_written_as_itself_not_escapes(self):
        path = self.writer.write(at(2026, 8, 31, 14, 5), "Клавдий")
        self.assertIn("Клавдий", path.read_text(encoding="utf-8"))

    def test_the_transcript_is_readable_only_by_its_owner(self):
        path = self.writer.write(at(2026, 8, 31, 14, 5), "secret")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_it_reports_where_it_writes(self):
        self.assertEqual(self.writer.describe(), str(self.root))


class ModeTests(unittest.TestCase):
    def test_a_fixed_file_skips_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "logs" / "everything.jsonl"
            writer = TranscriptWriter(None, target)
            self.assertEqual(writer.write(at(2026, 8, 31, 14, 0), "one"), target)
            self.assertEqual(writer.write(at(2026, 9, 1, 15, 0), "two"), target)
            self.assertEqual(len(target.read_text().splitlines()), 2)

    def test_a_fixed_file_wins_over_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "everything.jsonl"
            writer = TranscriptWriter(Path(tmp) / "rotated", target)
            self.assertEqual(writer.path_for(), target)
            self.assertFalse((Path(tmp) / "rotated").exists())

    def test_disabled_writes_nothing(self):
        writer = TranscriptWriter(None)
        self.assertFalse(writer.enabled)
        self.assertIsNone(writer.describe())
        self.assertIsNone(writer.path_for())
        self.assertIsNone(writer.write(time.time(), "into the void"))

    def test_paths_starting_with_a_tilde_are_expanded(self):
        writer = TranscriptWriter("~/.micclaude/transcripts")
        self.assertTrue(str(writer.describe()).startswith(str(Path.home())))

    def test_a_write_failure_is_logged_once_and_then_given_up_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "blocked"
            blocked.write_text("I am a file, not a directory")
            writer = TranscriptWriter(blocked)
            logging.disable(logging.CRITICAL)
            self.addCleanup(logging.disable, logging.NOTSET)
            self.assertIsNone(writer.write(time.time(), "one"))
            self.assertIsNone(writer.write(time.time(), "two"))
            self.assertTrue(writer._failed, "it stops retrying a hopeless target")


if __name__ == "__main__":
    unittest.main()
