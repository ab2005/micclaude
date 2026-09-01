"""A stand-in for whisper-server, for tests that need one to answer.

Mimics the parts micclaude uses: it listens, answers / so a readiness probe
succeeds, and answers /inference with JSON. Behaviour is steered by argv and
environment so a test can make it slow, broken, or dead on arrival.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEXT = os.environ.get("FAKE_WHISPER_TEXT", "hello there")
DELAY = float(os.environ.get("FAKE_WHISPER_STARTUP_DELAY", "0"))
FAIL = os.environ.get("FAKE_WHISPER_FAIL")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"whisper.cpp", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path.rstrip("/") != "/inference":
            return self._send(404, b'{"error": "no such path"}')
        if os.environ.get("FAKE_WHISPER_HTTP_ERROR"):
            return self._send(500, b'{"error": "model exploded"}')
        if os.environ.get("FAKE_WHISPER_GARBAGE"):
            return self._send(200, b"not json at all")
        # Echo enough for a test to check the upload arrived intact.
        self._send(200, json.dumps({"text": TEXT, "bytes": len(body)}).encode())


def main() -> int:
    port = 8181
    argv = sys.argv[1:]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if FAIL:
        print(f"fatal: {FAIL}", file=sys.stderr, flush=True)
        return 1
    if DELAY:
        time.sleep(DELAY)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
