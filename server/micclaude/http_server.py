"""The local web server: static page, transcription endpoint, Claude endpoint.

Everything is stdlib. The browser does capture, segmentation, wake-word
detection and speech synthesis; this server only turns audio into text and
relays questions to the Claude Code CLI.
"""

from __future__ import annotations

import itertools
import json
import logging
import mimetypes
import os
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from . import __version__
from .broadcast import Broadcaster
from .claude_client import ClaudeClient, ClaudeNotFound, ClaudeReply, Delta, format_prompt
from .config import Config
from .notes import Notes
from .observer import Observer
from .transcribe import Transcriber, TranscriptionError, build_transcriber, decode_wav
from .transcript import TranscriptWriter

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def find_web_root() -> Path:
    """Locate the front end: an override, an installed copy, or the repo."""
    override = os.environ.get("MICCLAUDE_WEB_ROOT")
    candidates = [Path(override)] if override else []
    package = Path(__file__).resolve().parent
    candidates += [package / "web", package.parents[1] / "web"]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "could not find the web/ directory. Run micclaude from a checkout, "
        "install it with 'pip install -e .', or set MICCLAUDE_WEB_ROOT."
    )


@dataclass
class TranscriptEntry:
    timestamp: float
    text: str
    id: int = 0
    source: str = "browser"
    """Where the text came from: the page's own microphone, or a recorder."""

    speaker: str | None = None
    """Who said it, when the transcriber can tell. Usually nobody can."""

    client: str | None = None
    """Opaque id of whoever posted it, so a page can ignore its own echo."""

    def format(self) -> str:
        return f"[{time.strftime('%H:%M:%S', time.localtime(self.timestamp))}] {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "time": self.timestamp,
            "text": self.text,
            "source": self.source,
            "speaker": self.speaker,
            "client": self.client,
        }


class App:
    """Server state shared by all requests."""

    def __init__(
        self,
        config: Config,
        *,
        transcriber: Transcriber | None = None,
        claude: ClaudeClient | None = None,
        web_root: Path | None = None,
    ) -> None:
        self.config = config
        self.transcriber = transcriber or build_transcriber(config.transcribe)
        self.claude = claude or ClaudeClient(config.claude)
        self.web_root = (web_root or find_web_root()).resolve()
        self.transcript: list[TranscriptEntry] = []
        self.events = Broadcaster()
        self.observer = Observer(
            config.observer,
            self.claude,
            notes=Notes.load(config.observer.notes_file) if config.observer.notes_file else Notes(),
            publish=self.events.publish,
        )
        self._ids = itertools.count(1)
        self._transcribe_lock = threading.Lock()
        self._transcript_lock = threading.Lock()
        self.writer = TranscriptWriter(config.transcript_dir, config.transcript_file)

    # ------------------------------------------------------------- behaviour

    def transcribe(self, wav: bytes) -> tuple[str, float]:
        """Return (text, audio duration in ms) for one uploaded utterance."""
        utterance = decode_wav(wav, target_rate=self.config.audio.sample_rate)
        with self._transcribe_lock:
            text = self.transcriber.transcribe(utterance)
        return text.strip(), utterance.duration_ms

    def record(
        self,
        text: str,
        *,
        source: str = "browser",
        client: str | None = None,
        timestamp: float | None = None,
        speaker: str | None = None,
    ) -> TranscriptEntry:
        """Store one recognized utterance and tell every open page about it."""
        entry = TranscriptEntry(
            timestamp=timestamp if timestamp is not None else time.time(),
            text=text,
            id=next(self._ids),
            source=source,
            speaker=speaker,
            client=client,
        )
        with self._transcript_lock:
            self.transcript.append(entry)
            del self.transcript[:-500]
            self.writer.write(entry.timestamp, text)
        self.events.publish("utterance", entry.to_dict())
        self.observer.add(text, entry.timestamp, speaker=entry.speaker)
        return entry

    def recent(self, limit: int = 100) -> list[TranscriptEntry]:
        """The last entries, so a page that just opened can catch up."""
        with self._transcript_lock:
            return self.transcript[-limit:]

    def context_lines(self, supplied: list[str] | None = None) -> list[str]:
        """Recent transcript lines to send with a question.

        The browser holds the authoritative transcript, so it may pass its own
        lines; otherwise fall back to what this server has recorded.
        """
        count = self.config.claude.include_context_lines
        if count <= 0:
            return []
        if supplied:
            return [line for line in supplied if line.strip()][-count:]
        with self._transcript_lock:
            return [entry.format() for entry in self.transcript[-count - 1 : -1]][-count:]

    def health(self) -> dict[str, Any]:
        try:
            claude_path: str | None = self.claude.resolve_binary()
            claude_error = None
        except ClaudeNotFound as exc:
            claude_path, claude_error = None, str(exc)
        return {
            "ok": claude_error is None,
            "version": __version__,
            "backend": getattr(self.transcriber, "name", "unknown"),
            "model": self.config.transcribe.model,
            "claudePath": claude_path,
            "claudeError": claude_error,
            "claudeModel": self.config.claude.model,
            "workingDir": self.config.claude.working_dir or str(Path.cwd()),
            "transcriptPath": self.writer.describe(),
            "observing": self.config.observer.enabled,
            "sessionActive": bool(self.claude.session_id),
        }

    def client_settings(self) -> dict[str, Any]:
        """Defaults the browser needs: capture, wake words, speech."""
        return self.config.client_settings()

    def warm_up(self) -> None:
        """Load the speech model before the first question arrives."""
        loader = getattr(self.transcriber, "load", None)
        if callable(loader):
            loader()
        self.observer.start()

    def shutdown(self) -> None:
        """Send whatever the observer still holds, then let go."""
        self.observer.stop()
        self.events.close()
        stop = getattr(self.transcriber, "stop", None)
        if callable(stop):
            stop()  # a whisper-server we started is ours to stop


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"micclaude/{__version__}"
    app: App  # set by create_server

    # ------------------------------------------------------------- plumbing

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - %s", self.address_string(), format % args)

    def _origin_allowed(self) -> bool:
        """Refuse requests from other origins, and Host headers we do not expect.

        The server holds a live microphone feed and a Claude session, so a page
        on another origin must not be able to drive it.
        """
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname or ""
        expected = self.app.config.server.host
        if host and host not in LOOPBACK_HOSTS and host != expected:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlparse(origin)
        return (parsed.hostname or "") in LOOPBACK_HOSTS or parsed.hostname == expected

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"upload too large ({length} bytes)")
        return self.rfile.read(length) if length else b""

    # --------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(self.app.health())
        if path == "/api/settings":
            return self._json(self.app.client_settings())
        if path == "/api/events":
            return self._events()
        if path == "/api/notes":
            return self._json(
                {
                    "notes": self.app.observer.notes.to_dict(),
                    "counts": self.app.observer.counts(),
                    "pending": self.app.observer.pending,
                    "enabled": self.app.config.observer.enabled,
                }
            )
        if path == "/api/notes.md":
            body = self.app.observer.notes.to_markdown(self.app.config.language).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "text/markdown; charset=utf-8")
        if path == "/api/transcript":
            return self._json({"entries": [e.to_dict() for e in self.app.recent()]})
        if path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")
        return self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))

        if path == "/api/transcribe":
            return self._transcribe(body)
        if path == "/api/utterance":
            return self._utterance(body)
        if path == "/api/ask":
            return self._ask(body)
        if path == "/api/session/reset":
            self.app.claude.reset()
            return self._json({"ok": True})
        if path == "/api/notes/finish":
            try:
                summary = self.app.observer.finish(language=self.app.config.language)
            except Exception as exc:
                log.exception("finishing the meeting failed")
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"could not finish: {exc}")
            return self._json(
                {
                    "summary": summary.text,
                    "path": str(summary.path) if summary.path else None,
                    "counts": self.app.observer.counts(),
                    "error": summary.error,
                }
            )
        if path == "/api/notes/clear":
            self.app.observer.clear()
            return self._json({"ok": True, "counts": self.app.observer.counts()})
        if path == "/api/notes/flush":
            try:
                result = self.app.observer.flush()
            except Exception as exc:  # a bad batch must not kill the connection
                log.exception("flushing the observer failed")
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"batch failed: {exc}")
            return self._json(
                {
                    "sent": result is not None,
                    "counts": self.app.observer.counts(),
                    "error": getattr(result, "error", None),
                }
            )
        return self._error(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

    # -------------------------------------------------------------- handlers

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (self.app.web_root / relative).resolve()
        if not target.is_relative_to(self.app.web_root) or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def _transcribe(self, body: bytes) -> None:
        if not body:
            return self._error(HTTPStatus.BAD_REQUEST, "empty audio upload")
        started = time.monotonic()
        try:
            text, duration_ms = self.app.transcribe(body)
        except TranscriptionError as exc:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except Exception as exc:  # pragma: no cover - backend specific
            log.exception("transcription failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"transcription failed: {exc}")
        if text:
            self.app.record(text, source="browser", client=self.headers.get("X-Client-Id"))
        self._json(
            {
                "text": text,
                "audioMs": round(duration_ms),
                "elapsedMs": round((time.monotonic() - started) * 1000),
            }
        )

    def _utterance(self, body: bytes) -> None:
        """Text recognized somewhere else -- a recorder process, or a script."""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._error(HTTPStatus.BAD_REQUEST, "expected a JSON body")
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._error(HTTPStatus.BAD_REQUEST, "'text' is required")
        timestamp = payload.get("time")
        speaker = payload.get("speaker")
        entry = self.app.record(
            text,
            source=str(payload.get("source") or "recorder"),
            client=self.headers.get("X-Client-Id"),
            timestamp=float(timestamp) if isinstance(timestamp, (int, float)) else None,
            speaker=str(speaker).strip() or None if speaker else None,
        )
        self._json(entry.to_dict(), HTTPStatus.CREATED)

    def _events(self) -> None:
        """Server-sent events: everything recognized, whoever recognized it."""
        with self.app.events.subscribe() as subscription:
            self._stream_sse(subscription.events())

    def _ask(self, body: bytes) -> None:
        try:
            payload = json.loads(body or b"{}")
            question = str(payload["question"]).strip()
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._error(HTTPStatus.BAD_REQUEST, "expected JSON with a 'question' field")
        if not question:
            return self._error(HTTPStatus.BAD_REQUEST, "question is empty")
        context = payload.get("context")
        context = [str(line) for line in context] if isinstance(context, list) else None
        prompt = format_prompt(question, self.app.context_lines(context))
        self.app.observer.note_question()
        self._stream_sse(self._claude_events(prompt))

    def _claude_events(self, prompt: str) -> Iterator[tuple[str, dict[str, Any]]]:
        try:
            for item in self.app.claude.stream(prompt):
                if isinstance(item, Delta):
                    yield "delta", {"text": item.text}
                elif isinstance(item, ClaudeReply):
                    yield ("error" if item.is_error else "done"), {
                        "text": item.text,
                        "sessionId": item.session_id,
                        "durationMs": item.duration_ms,
                        "costUsd": item.cost_usd,
                    }
        except ClaudeNotFound as exc:
            yield "error", {"text": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("claude call failed")
            yield "error", {"text": f"claude call failed: {exc}"}

    def _stream_sse(self, events: Iterator[tuple[str, dict[str, Any]] | None]) -> None:
        """Write an event stream until it ends or the client goes away.

        A ``None`` from the iterator is a keepalive: an SSE comment, which the
        browser ignores but a dead connection cannot swallow.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for event in events:
            if event is None:
                chunk = b": keepalive\n\n"
            else:
                name, data = event
                chunk = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.info("client disconnected")
                return


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A browser that navigates away mid-stream is not an error."""
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            log.debug("client %s disconnected", client_address)
            return
        super().handle_error(request, client_address)


def create_server(
    config: Config,
    *,
    transcriber: Transcriber | None = None,
    claude: ClaudeClient | None = None,
    web_root: Path | None = None,
) -> tuple["Server", App]:
    """Build a server bound to the configured address (port 0 picks a free one)."""
    app = App(config, transcriber=transcriber, claude=claude, web_root=web_root)
    handler = type("BoundHandler", (Handler,), {"app": app})
    return Server((config.server.host, config.server.port), handler), app


def serve(
    config: Config,
    *,
    transcriber: Transcriber | None = None,
    claude: ClaudeClient | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> int:
    server, app = create_server(config, transcriber=transcriber, claude=claude)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}/"
    app.warm_up()
    if on_ready:
        on_ready(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.shutdown()
        server.server_close()
    return 0
