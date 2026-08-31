"""Configuration for the local micclaude server.

Values come from dataclass defaults, then an optional TOML file, then command
line flags. The ``audio``, ``trigger`` and ``speech`` sections are handed to the
browser by ``GET /api/config``: capture, wake-word detection and speech are all
done client side, and the server only holds the defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import languages


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    """Loopback only by default. The page needs a secure context, and
    ``localhost`` counts as one; exposing this on a LAN address would not."""

    port: int = 8765
    open_browser: bool = True


@dataclass
class AudioConfig:
    """Browser-side capture and utterance segmentation."""

    sample_rate: int = 16000
    frame_ms: int = 30
    energy_threshold: float = 0.015
    """RMS (0..1) above which a frame counts as speech."""

    start_frames: int = 3
    silence_ms: int = 700
    """Trailing silence that closes an utterance."""

    preroll_ms: int = 300
    min_utterance_ms: int = 350
    max_utterance_ms: int = 30000


@dataclass
class TranscribeConfig:
    """Server-side speech to text."""

    backend: str = "faster-whisper"
    """``faster-whisper`` (local, default), ``openai`` (HTTP API), or ``null``."""

    model: str = "base.en"
    device: str = "auto"
    compute_type: str = "int8"
    language: str | None = "en"
    beam_size: int = 1
    initial_prompt: str | None = "The speaker addresses an assistant named Claude."
    """Biasing text; naming the wake word here helps Whisper spell it right."""

    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    api_model: str = "whisper-1"
    api_timeout: float = 60.0


@dataclass
class TriggerConfig:
    """What counts as explicitly addressing Claude. Evaluated in the browser."""

    wake_words: list[str] = field(default_factory=lambda: ["claude"])
    aliases: list[str] = field(default_factory=lambda: ["cloud", "claud", "clawed", "clod"])
    """Extra spellings that count as the wake word: speech-to-text rarely gets
    a name right the first time. Fuzzy matching widens each wake word further."""

    prefixes: list[str] = field(
        default_factory=lambda: ["hey", "ok", "okay", "hi", "hello", "yo"]
    )
    require_prefix: bool = False
    fuzzy: bool = True
    max_wake_distance: int = 1
    fuzzy_min_length: int = 6
    """Only wake words at least this long are widened by edit distance. Short
    names collide badly: "клод" is one edit from "код", which nobody means as
    a wake word."""
    scan_window_words: int = 4
    """The wake word must appear within the first N words of an utterance."""

    cancel_phrases: list[str] = field(
        default_factory=lambda: ["never mind", "nevermind", "cancel that", "forget it"]
    )
    filler: list[str] = field(
        default_factory=lambda: ["please", "um", "uh", "so", "well", "hey", "okay", "ok"]
    )
    """Dropped from the start of a question: "claude, please open the file"."""

    min_prompt_chars: int = 2


@dataclass
class ClaudeConfig:
    """How to shell out to the Claude Code CLI."""

    binary: str = "claude"
    working_dir: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    timeout: float = 180.0

    continue_session: bool = True
    """Resume the same Claude session across questions so follow-ups have context."""

    append_system_prompt: str = (
        "You are answering over a live voice link. The user's words reached you through "
        "speech-to-text, so expect homophones and missing punctuation, and ask for a "
        "repeat when a request is genuinely ambiguous. Answer in the language the user "
        "spoke. Reply in at most a few sentences of plain spoken prose: no markdown, no "
        "bullet lists, no code blocks unless the user explicitly asks to hear code."
    )

    include_context_lines: int = 6
    """Recent transcript lines sent with each question. 0 sends only the question."""


@dataclass
class SpeechConfig:
    """Browser speech synthesis for replies."""

    enabled: bool = True
    lang: str = "en-US"
    """BCP-47 tag handed to the browser, which picks a voice for it."""

    voice: str | None = None
    rate: float = 1.0
    max_chars: int = 700


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)

    language: str = "en"
    """Spoken language. Sets the speech model, the wake word and the phrases
    that cancel a question; see micclaude/languages.py. Anything set
    explicitly still wins over the preset."""

    transcript_file: str | None = None
    """Append every recognized utterance here as JSON lines."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        """Catch settings that cannot work before the server starts."""
        if self.language != "en" and languages.is_english_only(self.transcribe.model):
            preset = languages.PRESETS.get(self.language)
            suggestion = preset.model if preset else "small"
            raise ConfigError(
                f"speech model '{self.transcribe.model}' is English-only, but the language "
                f"is '{self.language}'. Use a multilingual model, for example '{suggestion}'."
            )

    def client_settings(self) -> dict[str, Any]:
        """The subset the browser needs. Never includes CLI paths or tool grants."""
        return {
            "audio": asdict(self.audio),
            "trigger": asdict(self.trigger),
            "speech": asdict(self.speech),
            "contextLines": self.claude.include_context_lines,
            "language": self.language,
        }


DEFAULT_CONFIG_NAMES = ("micclaude.toml", ".micclaude.toml")


class ConfigError(ValueError):
    """Unknown key or wrong value type in a config file."""


def find_config_file(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    for directory in (Path.cwd(), Path.home()):
        for name in DEFAULT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def apply_language(config: Config, code: str) -> Config:
    """Set everything a spoken language implies. Explicit settings win later."""
    preset = languages.get(code)
    config.language = preset.code
    config.transcribe.language = preset.code
    config.transcribe.model = preset.model
    config.transcribe.initial_prompt = preset.initial_prompt
    config.trigger.wake_words = list(preset.wake_words)
    config.trigger.aliases = list(preset.aliases)
    config.trigger.prefixes = list(preset.prefixes)
    config.trigger.cancel_phrases = list(preset.cancel_phrases)
    config.trigger.filler = list(preset.filler)
    config.speech.lang = preset.bcp47
    return config


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    config = Config()
    if path is None:
        return config
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    language = data.get("language")
    if language is not None:
        if not isinstance(language, str):
            raise ConfigError(f"{path}: 'language' must be a string")
        try:
            apply_language(config, language)
        except KeyError as exc:
            raise ConfigError(f"{path}: {exc.args[0]}") from exc
    _apply(config, data, path=str(path))
    return config


def _apply(target: Any, data: dict[str, Any], *, path: str, prefix: str = "") -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"{path}: unknown setting '{prefix}{key}'")
        current = getattr(target, key)
        if is_dataclass(current) and not isinstance(current, type):
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: '{prefix}{key}' must be a table")
            _apply(current, value, path=path, prefix=f"{prefix}{key}.")
            continue
        setattr(target, key, _coerce(known[key], value, path, f"{prefix}{key}"))


def _coerce(field_def: Any, value: Any, path: str, location: str) -> Any:
    annotation = str(field_def.type)
    if "list[str]" in annotation:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{path}: '{location}' must be a list of strings")
        return list(value)
    if annotation.startswith("bool") and not isinstance(value, bool):
        raise ConfigError(f"{path}: '{location}' must be true or false")
    if annotation.startswith("int") and isinstance(value, float):
        raise ConfigError(f"{path}: '{location}' must be an integer")
    if annotation.startswith("float") and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value
