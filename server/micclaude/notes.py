"""The running notes a meeting produces.

The notes live outside Claude's session on purpose. Inside the session they
could not be shown, checked or kept: a document on disk can be read while the
meeting is still going, survives the session dying, and -- because every entry
carries the line it came from -- can be verified against what was actually
said.

Claude returns a *delta* per batch rather than the whole document: cheaper,
steadier, and it leaves a history of when each point appeared.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SECTIONS = ("points", "decisions", "tasks", "questions", "flags")
"""The lists a delta may add to. Anything else in a reply is ignored."""

HEADINGS = {
    "en": {
        "untitled": "Meeting",
        "due": "by",
        "summary": "Summary",
        "sections": {
            "points": "Points",
            "decisions": "Decisions",
            "tasks": "Tasks",
            "questions": "Open questions",
            "flags": "Flagged",
        },
    },
    "ru": {
        "untitled": "Встреча",
        "due": "срок:",
        "summary": "Итоги",
        "sections": {
            "points": "Тезисы",
            "decisions": "Решения",
            "tasks": "Задачи",
            "questions": "Открытые вопросы",
            "flags": "Замечено",
        },
    },
}
"""Section headings per language. The JSON keys stay English so the reply can
be parsed the same way whatever is being spoken."""


@dataclass
class Entry:
    """One line of the notes, anchored to the speech it came from."""

    text: str
    quote: str = ""
    """The words that justify this entry. Without it nothing is checkable."""

    time: float | None = None
    who: str | None = None
    due: str | None = None
    rule: str | None = None
    """For a flag: which standing instruction fired."""

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "")}


@dataclass
class Notes:
    """Everything the meeting has produced so far."""

    started: float = field(default_factory=time.time)
    title: str = ""
    points: list[Entry] = field(default_factory=list)
    decisions: list[Entry] = field(default_factory=list)
    tasks: list[Entry] = field(default_factory=list)
    questions: list[Entry] = field(default_factory=list)
    flags: list[Entry] = field(default_factory=list)

    # ----------------------------------------------------------------- shape

    def section(self, name: str) -> list[Entry]:
        return getattr(self, name)

    @property
    def is_empty(self) -> bool:
        return not any(self.section(name) for name in SECTIONS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "title": self.title,
            **{name: [entry.to_dict() for entry in self.section(name)] for name in SECTIONS},
        }

    # ----------------------------------------------------------------- delta

    def apply(self, delta: dict[str, Any]) -> dict[str, list[Entry]]:
        """Merge one reply into the notes. Returns only what was added.

        Unknown keys, wrong shapes and entries without text are dropped rather
        than raising: a malformed batch must not take the meeting down.
        """
        added: dict[str, list[Entry]] = {}
        if isinstance(delta.get("title"), str) and delta["title"].strip() and not self.title:
            self.title = delta["title"].strip()
        for name in SECTIONS:
            items = delta.get(name)
            if not isinstance(items, list):
                continue
            fresh = [entry for entry in map(_entry, items) if entry and not self._duplicate(name, entry)]
            if fresh:
                self.section(name).extend(fresh)
                added[name] = fresh
        return added

    def _duplicate(self, name: str, entry: Entry) -> bool:
        """A batch overlapping the previous one should not double an entry."""
        return any(existing.text.strip() == entry.text.strip() for existing in self.section(name))

    # ------------------------------------------------------------ persistence

    def save(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        target.chmod(0o600)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Notes":
        source = Path(path).expanduser()
        if not source.is_file():
            return cls()
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("cannot read notes from %s (%s); starting fresh", source, exc)
            return cls()
        notes = cls(started=float(data.get("started") or time.time()), title=str(data.get("title") or ""))
        for name in SECTIONS:
            notes.section(name).extend(entry for entry in map(_entry, data.get(name) or []) if entry)
        return notes

    # -------------------------------------------------------------- rendering

    def to_markdown(self, language: str = "en") -> str:
        """The notes as a document a person would read.

        Claude writes the content in the language being spoken; the headings
        are ours, so they have to follow it too.
        """
        words = HEADINGS.get(language, HEADINGS["en"])
        headings = words["sections"]
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        lines = [f"# {self.title or words['untitled']}", "", f"_{started}_", ""]
        for name in SECTIONS:
            entries = self.section(name)
            if not entries:
                continue
            lines += [f"## {headings[name]}", ""]
            for entry in entries:
                prefix = f"**{entry.who}** — " if entry.who else ""
                suffix = f" _({words['due']} {entry.due})_" if entry.due else ""
                lines.append(f"- {prefix}{entry.text}{suffix}")
                if entry.quote:
                    lines.append(f"  > {entry.quote}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _entry(item: Any) -> Entry | None:
    """Build an entry from whatever the model returned, or None."""
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    when = item.get("time")
    return Entry(
        text=text,
        quote=str(item.get("quote") or "").strip(),
        time=float(when) if isinstance(when, (int, float)) else None,
        who=_optional(item.get("who")),
        due=_optional(item.get("due")),
        rule=_optional(item.get("rule")),
    )


def _optional(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
