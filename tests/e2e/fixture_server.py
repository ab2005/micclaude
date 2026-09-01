"""Start a micclaude server with stubbed speech and a stubbed Claude CLI.

Used by the browser tests: the whole stack runs for real -- HTTP, SSE, the
subprocess, the page -- but transcription returns a fixed sentence and the
"CLI" is a shell script, so the test is deterministic and free.

Prints the base URL on stdout, then serves until killed.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from micclaude.claude_client import ClaudeClient  # noqa: E402
from micclaude.config import Config, apply_language  # noqa: E402
from micclaude.http_server import create_server  # noqa: E402
from micclaude.transcribe import Utterance  # noqa: E402

HEARD = os.environ.get("MICCLAUDE_FAKE_TRANSCRIPT", "hey claude, what is this?")
LANGUAGE = os.environ.get("MICCLAUDE_LANG", "en")
OBSERVE = os.environ.get("MICCLAUDE_OBSERVE") == "1"

FAKE_CLI = r'''#!/usr/bin/env python3
"""A stand-in for the Claude CLI, answering the way each caller expects.

Questions get a streamed echo; an observer batch gets a JSON delta quoting the
transcript it was given; the closing request gets prose.
"""

import json
import sys


def emit(text, session="e2e-1"):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}))
    for chunk in (text[: len(text) // 2], text[len(text) // 2 :]):
        print(json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": chunk},
            },
        }))
    print(json.dumps({
        "type": "result", "subtype": "success", "result": text,
        "session_id": session, "duration_ms": 42, "total_cost_usd": 0.001,
    }))


def main():
    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""

    if "not a batch" in prompt:
        emit("Обсудили падающие тесты и договорились добавить healthcheck.")
        return

    if "<transcript>" in prompt:
        body = prompt.split("<transcript>", 1)[1].split("</transcript>", 1)[0]
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        spoken = [line.split("] ", 1)[-1] for line in lines]
        delta = {
            "title": "Test meeting",
            "points": [{"text": f"noted: {text}", "quote": text} for text in spoken[:2]],
            "flags": [{"text": "a rule fired", "rule": "test rule", "quote": spoken[0]}] if spoken else [],
            "say": None,
        }
        emit(json.dumps(delta, ensure_ascii=False))
        return

    emit(f"You asked: {prompt}")


main()
'''


class FixedTranscriber:
    name = "stub"

    def load(self) -> None:
        pass

    def transcribe(self, utterance: Utterance) -> str:
        return HEARD


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="micclaude-e2e-")
    cli = Path(tmp) / "claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    config = apply_language(Config(), LANGUAGE)
    config.transcript_dir = str(Path(tmp) / "transcripts")
    config.server.port = int(os.environ.get("MICCLAUDE_PORT", "0"))
    config.claude.binary = str(cli)
    config.claude.append_system_prompt = ""
    config.audio.energy_threshold = 0.005  # the browser's fake device is quiet
    config.audio.min_utterance_ms = 200
    config.observer.enabled = OBSERVE
    config.observer.min_lines = 1
    config.observer.notes_file = str(Path(tmp) / "notes.json")

    server, _app = create_server(config, transcriber=FixedTranscriber(), claude=ClaudeClient(config.claude))
    print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
