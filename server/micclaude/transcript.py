"""Writing recognized speech to disk.

Everything the microphone hears is appended as JSON lines, rotated into one
file per hour inside a directory per day:

    ~/.micclaude/transcripts/2026-08-31/14.jsonl

Day and hour come from local time, because that is how a person looks for
"what did I say yesterday afternoon". The timestamp inside each record stays
epoch seconds.

The file is opened for each line rather than held open. Utterances arrive a few
times a minute at most, so the cost is irrelevant, and nothing is lost if the
process is killed or the directory is moved out from under it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
"""A transcript holds everything said near the microphone: keep it to its owner."""


class TranscriptWriter:
    """Appends utterances to a rotated transcript, or nowhere when disabled."""

    def __init__(self, directory: str | os.PathLike[str] | None, file: str | os.PathLike[str] | None = None) -> None:
        self.file = Path(file).expanduser() if file else None
        self.directory = Path(directory).expanduser() if directory and not self.file else None
        self._ready: set[Path] = set()
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.file is not None or self.directory is not None

    def describe(self) -> str | None:
        """Where the transcript is going, for the settings panel."""
        target = self.file or self.directory
        return str(target) if target else None

    def path_for(self, when: float | None = None) -> Path | None:
        """The file this moment's speech belongs in."""
        if self.file is not None:
            return self.file
        if self.directory is None:
            return None
        moment = time.localtime(when if when is not None else time.time())
        return self.directory / time.strftime("%Y-%m-%d", moment) / time.strftime("%H.jsonl", moment)

    def write(self, timestamp: float, text: str) -> Path | None:
        """Append one utterance. Returns the file written, or None."""
        path = self.path_for(timestamp)
        if path is None or self._failed:
            return None
        line = json.dumps({"time": timestamp, "text": text}, ensure_ascii=False)
        try:
            self._prepare(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # A full or read-only disk must not take the assistant down with it.
            log.error("cannot write the transcript to %s: %s; not trying again", path, exc)
            self._failed = True
            return None
        return path

    def _prepare(self, path: Path) -> None:
        """Create the day directory and the file with owner-only permissions."""
        if path in self._ready:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
        if not path.exists():
            path.touch(mode=FILE_MODE)
        self._ready.add(path)
