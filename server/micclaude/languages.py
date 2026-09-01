"""Language presets.

A spoken language decides more than the Whisper flag: it decides which model
can be used at all (the ``.en`` models are English-only), how the wake word is
likely to come back from speech-to-text, and which words cancel a pending
question. A preset sets all of it at once, and any explicit setting still wins
over what the preset chose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguagePreset:
    code: str
    """Whisper language code."""

    label: str
    """Endonym, shown in the page."""

    bcp47: str
    """Tag for speech synthesis, so the browser picks a matching voice."""

    model: str
    """Default speech model. Multilingual outside English."""

    initial_prompt: str
    """Biases the decoder toward spelling the wake word the way we expect."""

    wake_words: list[str]
    aliases: list[str] = field(default_factory=list)
    """Spellings speech-to-text produces for the same name."""

    prefixes: list[str] = field(default_factory=list)
    cancel_phrases: list[str] = field(default_factory=list)
    filler: list[str] = field(default_factory=list)
    """Words dropped from the start of a question: "claude, please ..."."""


ENGLISH = LanguagePreset(
    code="en",
    label="English",
    bcp47="en-US",
    model="base.en",
    initial_prompt="The speaker addresses an assistant named Claude.",
    wake_words=["claude"],
    aliases=["cloud", "claud", "clawed", "clod"],
    prefixes=["hey", "ok", "okay", "hi", "hello", "yo"],
    cancel_phrases=["never mind", "nevermind", "cancel that", "forget it"],
    filler=["please", "um", "uh", "so", "well", "hey", "okay", "ok"],
)

RUSSIAN = LanguagePreset(
    code="ru",
    label="Русский",
    bcp47="ru-RU",
    # base is multilingual but weak on Russian; small is the smallest size
    # that transcribes conversational Russian reliably.
    model="small",
    initial_prompt="Говорящий обращается к ассистенту по имени Клавдий.",
    # "Клавдий" rather than "Клод": seven letters, so fuzzy matching can widen
    # it safely. Four-letter "Клод" is one edit from "код", which is said
    # constantly when talking about code.
    wake_words=["клавдий"],
    # Case forms and the spellings Whisper produces, including the Latin one it
    # sometimes keeps mid-Russian. Aliases match exactly, so listing the short
    # "клод" here costs nothing.
    aliases=[
        "клавдия",
        "клавдию",
        "клавдие",
        "клавдии",
        "клаудий",
        "клаудио",
        "клавдей",
        "клод",
        "claude",
    ],
    prefixes=["эй", "ок", "окей", "привет", "слушай", "слышь", "хэй"],
    cancel_phrases=["отмена", "отменить", "неважно", "не важно", "забудь", "забей", "отбой", "проехали"],
    filler=["пожалуйста", "слушай", "короче", "ну", "эм", "э"],
)

PRESETS: dict[str, LanguagePreset] = {preset.code: preset for preset in (ENGLISH, RUSSIAN)}

ENGLISH_ONLY_SUFFIX = ".en"


def get(code: str) -> LanguagePreset:
    """Look up a preset, raising a helpful error for anything unsupported."""
    preset = PRESETS.get(code.lower().strip())
    if preset is None:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"no preset for language {code!r}; known languages: {known}")
    return preset


def is_english_only(model: str) -> bool:
    return model.strip().lower().endswith(ENGLISH_ONLY_SUFFIX)
