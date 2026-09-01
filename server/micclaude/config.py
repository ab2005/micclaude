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

    echo_cancellation: bool = True
    """Keeps the assistant from hearing its own spoken replies. Worth its cost."""

    noise_suppression: bool = False
    """The browser's suppressor is tuned for telephony intelligibility, not for
    a speech model, and it takes the consonants with the noise. Off by default:
    Whisper would rather have the hiss."""

    auto_gain: bool = False
    """Likewise: automatic gain pumps between phrases and smears quiet
    consonants. Turn it on only if your microphone is genuinely too quiet."""


@dataclass
class TranscribeConfig:
    """Server-side speech to text."""

    backend: str = "faster-whisper"
    """``faster-whisper`` (local, default), ``openai`` (HTTP API), or ``null``."""

    model: str = "base.en"
    device: str = "auto"
    compute_type: str = "int8"
    language: str | None = "en"

    beam_size: int = 5
    """Greedy decoding (1) is fastest and noticeably worse. On phrase-sized
    audio the extra cost is small, so accuracy wins by default."""

    context_words: int = 24
    """Words of the previous phrase fed in as context. Whisper transcribes a
    short fragment much better when it knows what came just before, and
    unlike condition_on_previous_text this is bounded and under our control."""

    temperature: float = 0.0
    """0 is deterministic. faster-whisper falls back to higher values by
    itself when a decode looks degenerate."""

    vad_filter: bool = False
    """We segment on our own, but letting Whisper trim the edges too can help
    a noisy room. Costs a little latency."""
    initial_prompt: str | None = "The speaker addresses an assistant named Claude."
    """Biasing text; naming the wake word here helps Whisper spell it right."""

    condition_on_previous_text: bool = False
    """Whisper's default is to feed each chunk its own previous output, which
    makes one bad guess snowball into a repeating loop. We pass the previous
    phrase through initial_prompt instead: same benefit, bounded, and it
    cannot run away."""

    debug_audio_dir: str | None = None
    """Save every utterance as a WAV beside its text. Off by default -- it
    stores audio, which nothing else here does -- but it is the only way to
    tell bad recording from bad recognition."""

    drop_phrases: list[str] = field(
        default_factory=lambda: [
            "продолжение следует",
            "продолжение следует...",
            "спасибо за просмотр",
            "спасибо за просмотр!",
            "субтитры сделал dimatorzok",
            "редактор субтитров а.синецкая",
            "корректор а.егорова",
            "подписывайтесь на канал",
            "thanks for watching",
            "thanks for watching!",
            "thank you for watching",
            "subtitles by the amara.org community",
            "please subscribe",
            "you",
        ]
    )
    """Phrases Whisper hallucinates out of silence, learned from subtitle data.
    Matched against the whole utterance only -- someone really can say "thank
    you", and dropping that would be worse than the occasional phantom."""

    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    api_model: str = "whisper-1"
    api_timeout: float = 60.0

    # --- whisper.cpp -------------------------------------------------------
    # whisper.cpp is the fast path on Apple silicon: it uses Metal, which
    # CTranslate2 (faster-whisper) does not, so on a Mac it is the difference
    # between keeping up and falling behind. We talk to whisper-server rather
    # than the one-shot CLI because the CLI reloads the model on every run.

    server_url: str = "http://127.0.0.1:8181"
    server_binary: str = "whisper-server"
    autostart: bool = True
    """Start whisper-server ourselves and stop it on the way out. Turn this off
    to point at one you run yourself."""

    model_dir: str = "~/.cache/whisper.cpp"
    """Where ggml model files live, when `model` is a bare name like "small"."""

    server_args: list[str] = field(default_factory=list)
    """Extra flags for whisper-server, e.g. ["-t", "4"] for thread count."""

    startup_timeout: float = 120.0
    """Loading a large model on a cold cache is not quick."""


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
class ObserverConfig:
    """Claude following the conversation in batches, between questions."""

    enabled: bool = False
    """Off unless asked for: it spends the same budget your questions do."""

    interval_s: float = 150.0
    """Seconds between batches. Keep it under the prompt cache window, or
    every batch pays to re-read the conversation from scratch."""

    poll_s: float = 5.0
    """How often to check whether a batch is due."""

    min_lines: int = 2
    """Do not spend a turn on a single stray line."""

    max_lines: int = 40
    """Send early rather than let a fast conversation pile up."""

    quiet_after_question_s: float = 20.0
    """Hold batches back while a conversation with the assistant is live, so a
    spoken question is not stuck behind one."""

    notes_file: str | None = "~/.micclaude/notes.json"
    rules: list[str] = field(default_factory=list)
    """Standing instructions in plain language: what to watch for and flag."""

    speak_flags: bool = False
    """Read an urgent flag out loud. Off by default -- interrupting a meeting
    is a big thing to do on a misheard sentence."""


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
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)

    language: str = "en"
    """Spoken language. Sets the speech model, the wake word and the phrases
    that cancel a question; see micclaude/languages.py. Anything set
    explicitly still wins over the preset."""

    transcript_dir: str | None = "~/.micclaude/transcripts"
    """Where recognized speech is kept, as JSON lines rotated into one file per
    hour inside a directory per day. Set to nothing to keep no transcript."""

    transcript_file: str | None = None
    """One fixed file instead of the rotated directory, when you would rather
    manage rotation yourself."""

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
            "observer": {"enabled": self.observer.enabled, "speakFlags": self.observer.speak_flags},
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
