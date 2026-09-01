"""The recorder: a separate process that owns the microphone.

It captures, segments and transcribes locally, then posts text to a micclaude
server. The browser becomes a viewer that needs no microphone of its own, and
the machine doing the listening can be a different one from the machine you
work on.

Nothing recognized is lost if the server is down: undelivered lines go to a
spool file and are sent when it comes back.

    python -m micclaude.recorder --lang ru --server http://localhost:8765
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .capture import Microphone, list_devices
from .config import Config, ConfigError, apply_language, find_config_file, load_config
from .languages import PRESETS
from .transcribe import TranscriptionError, build_transcriber

log = logging.getLogger(__name__)

DEFAULT_SERVER = "http://127.0.0.1:8765"
DEFAULT_SPOOL = "~/.micclaude/pending.jsonl"
MAX_PENDING = 5000


class Delivery:
    """Posts recognized lines to the server, surviving it being unreachable.

    A failed line is kept and retried before the next one is sent, so an
    interrupted server costs latency rather than transcript.
    """

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        *,
        source: str = "recorder",
        spool: str | Path | None = DEFAULT_SPOOL,
        timeout: float = 10.0,
    ) -> None:
        self.url = server.rstrip("/") + "/api/utterance"
        self.source = source
        self.timeout = timeout
        self.spool = Path(spool).expanduser() if spool else None
        self.pending: list[dict[str, Any]] = []
        self.delivered = 0
        self._load_spool()

    # ------------------------------------------------------------------ spool

    def _load_spool(self) -> None:
        if self.spool is None or not self.spool.is_file():
            return
        for line in self.spool.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("text"):
                self.pending.append(entry)
        if self.pending:
            log.info("%d line(s) left over from last time; will resend", len(self.pending))

    def save_spool(self) -> None:
        """Write undelivered lines to disk, or remove the file when there are none."""
        if self.spool is None:
            return
        if not self.pending:
            self.spool.unlink(missing_ok=True)
            return
        self.spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in self.pending)
        self.spool.write_text(body + "\n", encoding="utf-8")

    # --------------------------------------------------------------- delivery

    def send(self, text: str, timestamp: float | None = None) -> bool:
        """Deliver one line, retrying anything left over first."""
        entry = {
            "text": text,
            "time": timestamp if timestamp is not None else time.time(),
            "source": self.source,
        }
        self.pending.append(entry)
        del self.pending[:-MAX_PENDING]
        return self.flush()

    def flush(self) -> bool:
        """Try to deliver everything pending, oldest first."""
        while self.pending:
            entry = self.pending[0]
            if not self._post(entry):
                return False
            self.pending.pop(0)
            self.delivered += 1
        return True

    def _post(self, entry: dict[str, Any]) -> bool:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(entry, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            # 4xx will not get better by retrying: drop it rather than wedge.
            if 400 <= exc.code < 500:
                log.error("server refused a line (%s); dropping it", exc.code)
                return True
            log.warning("server error %s; will retry", exc.code)
            return False
        except (urllib.error.URLError, OSError) as exc:
            log.warning("cannot reach %s (%s); holding %d line(s)", self.url, exc, len(self.pending))
            return False

    def close(self) -> None:
        self.flush()
        self.save_spool()


def record(config: Config, *, server: str, device: int | str | None, source: str, spool: str | None) -> int:
    """Capture, transcribe and deliver until interrupted."""
    transcriber = build_transcriber(config.transcribe)
    loader = getattr(transcriber, "load", None)
    if callable(loader):
        print(f"loading {config.transcribe.model}...", flush=True)
        loader()

    delivery = Delivery(server, source=source, spool=spool)
    microphone = Microphone(config.audio, device=device)
    microphone.start()
    print(f"recording; posting to {delivery.url}. Ctrl-C to stop.", flush=True)

    try:
        for utterance in microphone.utterances():
            try:
                text = transcriber.transcribe(utterance).strip()
            except TranscriptionError as exc:
                print(f"transcription failed: {exc}", file=sys.stderr, flush=True)
                continue
            if not text:
                continue
            ok = delivery.send(text)
            marker = "·" if ok else "!"
            print(f"{marker} {text}", flush=True)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        microphone.stop()
        delivery.close()
        if delivery.pending:
            print(
                f"{len(delivery.pending)} line(s) could not be delivered; "
                f"kept in {delivery.spool}",
                file=sys.stderr,
                flush=True,
            )
        if microphone.overflows:
            print(f"dropped {microphone.overflows} audio frame(s) under load", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="micclaude-recorder",
        description="Listen on a microphone, transcribe locally, post the text to micclaude.",
    )
    parser.add_argument("--version", action="version", version=f"micclaude {__version__}")
    parser.add_argument("-c", "--config", help="path to a TOML config file")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"where to post (default {DEFAULT_SERVER})")
    parser.add_argument("--lang", choices=sorted(PRESETS), help="spoken language preset")
    parser.add_argument("--model", help="whisper model, e.g. tiny.en, base.en, small")
    parser.add_argument("--backend", choices=["faster-whisper", "openai", "null"])
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument("--source", default="recorder", help="name this recorder in the transcript")
    parser.add_argument("--spool", default=DEFAULT_SPOOL, help="where to keep undelivered lines")
    parser.add_argument("--no-spool", action="store_true", help="do not keep undelivered lines")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.list_devices:
        print(list_devices())
        return 0

    try:
        config = load_config(find_config_file(args.config))
        if args.lang:
            apply_language(config, args.lang)
        if args.backend:
            config.transcribe.backend = args.backend
        if args.model:
            config.transcribe.model = args.model
        config.validate()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    device: int | str | None = None
    if args.device:
        device = int(args.device) if args.device.isdigit() else args.device

    try:
        return record(
            config,
            server=args.server,
            device=device,
            source=args.source,
            spool=None if args.no_spool else args.spool,
        )
    except TranscriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
