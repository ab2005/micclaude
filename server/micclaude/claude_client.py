"""Wrapper around the Claude Code CLI in headless (``-p``) mode.

Two shapes: :meth:`ClaudeClient.ask` for a single blocking answer, and
:meth:`ClaudeClient.stream` which yields text deltas as they arrive so the web
UI can render the reply while it is still being written.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .config import ClaudeConfig

log = logging.getLogger(__name__)


class ClaudeNotFound(RuntimeError):
    """The ``claude`` executable is not on PATH."""


@dataclass
class ClaudeReply:
    text: str
    session_id: str | None = None
    is_error: bool = False
    duration_ms: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class Delta:
    """A chunk of reply text."""

    text: str


class ClaudeClient:
    """Sends prompts to Claude Code and returns its answers.

    Questions reuse the previous session id (``--resume``) so spoken follow-ups
    such as "and what about the other one?" keep their context.
    """

    def __init__(self, config: ClaudeConfig | None = None) -> None:
        self.config = config or ClaudeConfig()
        self.session_id: str | None = None
        self._lock = threading.Lock()
        """One turn at a time: two concurrent ``--resume`` runs would race."""

    # ------------------------------------------------------------------ setup

    def resolve_binary(self) -> str:
        path = shutil.which(self.config.binary)
        if path is None:
            raise ClaudeNotFound(
                f"could not find '{self.config.binary}' on PATH. "
                "Install Claude Code (https://claude.com/claude-code) or set claude.binary."
            )
        return path

    def build_argv(self, prompt: str, *, stream: bool = False) -> list[str]:
        cfg = self.config
        argv = [cfg.binary, "-p", prompt, "--output-format"]
        argv += ["stream-json", "--include-partial-messages", "--verbose"] if stream else ["json"]
        if cfg.model:
            argv += ["--model", cfg.model]
        if cfg.permission_mode:
            argv += ["--permission-mode", cfg.permission_mode]
        if cfg.append_system_prompt:
            argv += ["--append-system-prompt", cfg.append_system_prompt]
        if cfg.allowed_tools:
            argv += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            argv += ["--disallowedTools", *cfg.disallowed_tools]
        if cfg.continue_session and self.session_id:
            argv += ["--resume", self.session_id]
        argv += list(cfg.extra_args)
        return argv

    def reset(self) -> None:
        """Forget the conversation; the next question starts a new session."""
        with self._lock:
            self.session_id = None

    # ------------------------------------------------------------- one-shot

    def ask(self, prompt: str) -> ClaudeReply:
        """Run one headless turn. Never raises for a failed turn; check ``is_error``."""
        self.resolve_binary()
        with self._lock:
            argv = self.build_argv(prompt)
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout,
                    cwd=self.config.working_dir or os.getcwd(),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ClaudeReply(
                    text=f"Claude did not answer within {self.config.timeout:.0f} seconds.",
                    is_error=True,
                )
            reply = parse_result(completed.stdout)
            if reply is None:
                detail = (completed.stderr or completed.stdout or "").strip()
                if self._forget_stale_session(detail):
                    return self.ask(prompt)
                return ClaudeReply(
                    text=detail.splitlines()[-1] if detail else "Claude returned nothing.",
                    is_error=True,
                )
            self._remember(reply)
            if completed.returncode != 0:
                reply.is_error = True
            return reply

    # -------------------------------------------------------------- streaming

    def stream(self, prompt: str) -> Iterator[Delta | ClaudeReply]:
        """Yield :class:`Delta` chunks as they arrive, then a final :class:`ClaudeReply`.

        Retries once without ``--resume`` if the stored session has gone away,
        which happens when Claude's own history is cleared between questions.
        """
        self.resolve_binary()
        with self._lock:
            yield from self._stream_once(prompt, allow_retry=True)

    def _stream_once(self, prompt: str, *, allow_retry: bool) -> Iterator[Delta | ClaudeReply]:
        argv = self.build_argv(prompt, stream=True)
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.config.working_dir or os.getcwd(),
        )
        timer = threading.Timer(self.config.timeout, process.kill)
        timer.start()
        final: ClaudeReply | None = None
        spoken = False
        try:
            for line in process.stdout or ():
                event = _parse_line(line)
                if event is None:
                    continue
                delta = text_delta(event)
                if delta:
                    spoken = True
                    yield Delta(delta)
                if event.get("type") == "result":
                    final = reply_from_event(event)
        finally:
            timer.cancel()
            stderr = (process.stderr.read() if process.stderr else "") or ""
            process.stdout and process.stdout.close()
            process.stderr and process.stderr.close()
            returncode = process.wait()

        if final is None:
            if allow_retry and not spoken and self._forget_stale_session(stderr):
                yield from self._stream_once(prompt, allow_retry=False)
                return
            message = stderr.strip().splitlines()[-1] if stderr.strip() else (
                f"Claude exited with status {returncode} before answering."
            )
            yield ClaudeReply(text=message, is_error=True)
            return
        self._remember(final)
        if returncode != 0:
            final.is_error = True
        yield final

    # ------------------------------------------------------------------ misc

    def _remember(self, reply: ClaudeReply) -> None:
        if reply.session_id and self.config.continue_session:
            self.session_id = reply.session_id

    def _forget_stale_session(self, stderr: str) -> bool:
        """True when the failure was a dead session id we should stop sending."""
        if not (self.config.continue_session and self.session_id):
            return False
        lowered = stderr.lower()
        if "no conversation found" in lowered or ("session" in lowered and "not found" in lowered):
            log.warning("session %s is gone; starting a fresh one", self.session_id)
            self.session_id = None
            return True
        return False


def _parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def text_delta(event: dict[str, Any]) -> str:
    """Pull assistant text out of a stream-json event, if it carries any."""
    if event.get("type") != "stream_event":
        return ""
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def reply_from_event(event: dict[str, Any]) -> ClaudeReply:
    text = event.get("result")
    if not isinstance(text, str):
        text = event.get("error") or event.get("message") or ""
        if not isinstance(text, str):
            text = json.dumps(text)
    return ClaudeReply(
        text=text.strip(),
        session_id=event.get("session_id"),
        is_error=bool(event.get("is_error")) or event.get("subtype") not in (None, "success"),
        duration_ms=event.get("duration_ms"),
        cost_usd=event.get("total_cost_usd"),
        raw=event,
    )


def parse_result(stdout: str) -> ClaudeReply | None:
    """Parse ``--output-format json`` output, tolerating a list or JSON lines."""
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        for line in reversed(stdout.splitlines()):
            event = _parse_line(line)
            if event and event.get("type") == "result":
                return reply_from_event(event)
        return None
    if isinstance(payload, list):
        results = [i for i in payload if isinstance(i, dict) and i.get("type") == "result"]
        if not results:
            return None
        payload = results[-1]
    return reply_from_event(payload) if isinstance(payload, dict) else None


def format_prompt(question: str, context: Sequence[str] = ()) -> str:
    """Wrap a spoken question with recent transcript lines for context."""
    question = question.strip()
    if not context:
        return question
    lines = "\n".join(context)
    return (
        "You are listening to a live microphone transcript. Here are the most recent lines "
        "for context (they were not necessarily addressed to you):\n"
        f"{lines}\n\n"
        "The user has now addressed you directly and asked:\n"
        f"{question}"
    )
