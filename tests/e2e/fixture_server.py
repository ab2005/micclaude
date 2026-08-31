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

FAKE_CLI = """
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) shift; PROMPT="$1";;
  esac
  shift
done
printf '%s\\n' '{"type":"system","subtype":"init","session_id":"e2e-1"}'
printf '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"You asked: "}}}\\n'
printf '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"%s"}}}\\n' "$PROMPT"
printf '{"type":"result","subtype":"success","result":"You asked: %s","session_id":"e2e-1","duration_ms":42,"total_cost_usd":0.001}\\n' "$PROMPT"
"""


class FixedTranscriber:
    name = "stub"

    def load(self) -> None:
        pass

    def transcribe(self, utterance: Utterance) -> str:
        return HEARD


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="micclaude-e2e-")
    cli = Path(tmp) / "claude"
    cli.write_text("#!/bin/sh\n" + FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    config = apply_language(Config(), LANGUAGE)
    config.server.port = int(os.environ.get("MICCLAUDE_PORT", "0"))
    config.claude.binary = str(cli)
    config.claude.append_system_prompt = ""
    config.audio.energy_threshold = 0.005  # the browser's fake device is quiet
    config.audio.min_utterance_ms = 200

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
