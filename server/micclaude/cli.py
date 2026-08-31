"""Command line entry point: ``python -m micclaude``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import webbrowser

from . import __version__
from .claude_client import ClaudeNotFound
from .config import Config, ConfigError, apply_language, find_config_file, load_config
from .http_server import serve
from .languages import PRESETS
from .transcribe import TranscriptionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="micclaude",
        description=(
            "Serve a local web app that transcribes your microphone and asks Claude Code "
            "when you address it by name."
        ),
    )
    parser.add_argument("--version", action="version", version=f"micclaude {__version__}")
    parser.add_argument("-c", "--config", help="path to a TOML config file")
    parser.add_argument("--print-config", action="store_true", help="show resolved config and exit")
    parser.add_argument(
        "--lang",
        choices=sorted(PRESETS),
        help=(
            "spoken language. Sets the speech model, the wake word, the cancel phrases "
            "and the voice in one go; later flags still win"
        ),
    )

    server = parser.add_argument_group("server")
    server.add_argument("--host", help="bind address (default 127.0.0.1; loopback is recommended)")
    server.add_argument("--port", type=int, help="port to listen on (default 8765; 0 picks one)")
    server.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    stt = parser.add_argument_group("transcription")
    stt.add_argument("--backend", choices=["faster-whisper", "openai", "null"])
    stt.add_argument("--model", help="whisper model, e.g. tiny.en, base.en, small.en, medium")
    stt.add_argument(
        "--stt-language",
        metavar="CODE",
        help=(
            "override just the recognition language with a Whisper code, or 'auto' to "
            "detect it per phrase. --lang already sets this; use it for a language with "
            "no preset"
        ),
    )

    claude = parser.add_argument_group("claude")
    claude.add_argument("--claude-model", help="model passed through to Claude Code")
    claude.add_argument("--claude-dir", metavar="DIR", help="directory Claude Code runs in")
    claude.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
    )
    claude.add_argument(
        "--allow-tool",
        action="append",
        metavar="TOOL",
        help="restrict Claude to these tools (repeatable), e.g. --allow-tool Read",
    )
    claude.add_argument("--no-context", action="store_true", help="send only the question")
    claude.add_argument(
        "--fresh-session", action="store_true", help="do not carry context between questions"
    )

    other = parser.add_argument_group("other")
    other.add_argument("--wake", action="append", metavar="WORD", help="wake word (repeatable)")
    other.add_argument(
        "--transcript-dir",
        metavar="DIR",
        help="where to keep the transcript (default ~/.micclaude/transcripts)",
    )
    other.add_argument(
        "--transcript",
        metavar="PATH",
        help="append everything to this one file instead of the rotated directory",
    )
    other.add_argument(
        "--no-transcript", action="store_true", help="do not write speech to disk at all"
    )
    other.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    # The language preset goes on first so that everything else overrides it.
    if args.lang:
        apply_language(config, args.lang)

    if args.host:
        config.server.host = args.host
    if args.port is not None:
        config.server.port = args.port
    if args.no_browser:
        config.server.open_browser = False

    if args.backend:
        config.transcribe.backend = args.backend
    if args.model:
        config.transcribe.model = args.model
    if args.stt_language:
        config.transcribe.language = None if args.stt_language == "auto" else args.stt_language

    if args.claude_model:
        config.claude.model = args.claude_model
    if args.claude_dir:
        config.claude.working_dir = args.claude_dir
    if args.permission_mode:
        config.claude.permission_mode = args.permission_mode
    if args.allow_tool:
        config.claude.allowed_tools = args.allow_tool
    if args.no_context:
        config.claude.include_context_lines = 0
    if args.fresh_session:
        config.claude.continue_session = False

    if args.wake:
        config.trigger.wake_words = args.wake
    if args.transcript_dir:
        config.transcript_dir = args.transcript_dir
    if args.transcript:
        config.transcript_file = args.transcript
    if args.no_transcript:
        config.transcript_dir = config.transcript_file = None
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        config = apply_overrides(load_config(find_config_file(args.config)), args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        config.validate()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_config:
        print(json.dumps(config.to_dict(), indent=2, default=str))
        return 0

    def announce(url: str) -> None:
        print(f"micclaude listening on {url}")
        print("Open it, allow the microphone, and say 'claude, ...' to ask a question.")
        if config.server.open_browser:
            threading.Timer(0.2, webbrowser.open, args=(url,)).start()

    try:
        return serve(config, on_ready=announce)
    except ClaudeNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TranscriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"error: cannot listen on {config.server.host}:{config.server.port}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
