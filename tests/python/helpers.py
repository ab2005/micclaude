"""Shared test helpers: stub backends and a fake ``claude`` executable."""

from __future__ import annotations

import array
import math
import os
import stat
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2] / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from micclaude.transcribe import Utterance  # noqa: E402


def tone(seconds: float = 0.5, rate: int = 16000, amplitude: float = 0.4) -> Utterance:
    count = int(seconds * rate)
    samples = array.array(
        "h", [int(amplitude * 32000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(count)]
    )
    return Utterance(pcm=samples.tobytes(), sample_rate=rate)


class StubTranscriber:
    """Returns a canned transcript and records what it was given."""

    name = "stub"

    def __init__(self, text: str = "hello there") -> None:
        self.text = text
        self.calls: list[Utterance] = []
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, utterance: Utterance) -> str:
        self.calls.append(utterance)
        return self.text


def write_fake_claude(directory: Path, body: str) -> Path:
    """Create a stub ``claude`` executable on disk and return its path."""
    path = directory / "claude"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def prepend_path(directory: Path) -> str:
    original = os.environ["PATH"]
    os.environ["PATH"] = f"{directory}{os.pathsep}{original}"
    return original


STREAMING_CLAUDE = """
cat <<'JSON'
{"type":"system","subtype":"init","session_id":"sess-1"}
{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}}
{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}}
{"type":"result","subtype":"success","result":"Hello world","session_id":"sess-1","duration_ms":120,"total_cost_usd":0.002}
JSON
"""
