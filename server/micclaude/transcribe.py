"""Speech to text backends.

The browser uploads 16-bit mono WAV and a backend turns it into text.
``faster-whisper`` is the default and runs entirely on this machine, so the
audio never leaves it. An OpenAI-compatible HTTP backend is available for
people who would rather not run a model locally, and a ``null`` backend keeps
the server testable without either.
"""

from __future__ import annotations

import array
import io
import json
import logging
import math
import os
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from typing import Protocol

from .config import TranscribeConfig

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Utterance:
    """One phrase of 16-bit mono PCM."""

    pcm: bytes
    sample_rate: int

    @property
    def duration_ms(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate * 1000

    def to_wav(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(self.pcm)
        return buffer.getvalue()

    def resample(self, rate: int) -> "Utterance":
        """Linear resample. Fine for speech at these ratios, and dependency free."""
        if rate == self.sample_rate or not self.pcm:
            return self
        samples = array.array("h")
        samples.frombytes(self.pcm)
        ratio = self.sample_rate / rate
        count = max(1, int(len(samples) / ratio))
        out = array.array("h", bytes(2 * count))
        for i in range(count):
            position = i * ratio
            left = int(position)
            right = min(left + 1, len(samples) - 1)
            weight = position - left
            out[i] = int(samples[left] * (1 - weight) + samples[right] * weight)
        return Utterance(pcm=out.tobytes(), sample_rate=rate)


def rms(pcm: bytes) -> float:
    """Root-mean-square level of int16 audio, normalized to 0..1."""
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples)) / 32768.0


def decode_wav(data: bytes, *, target_rate: int | None = None) -> Utterance:
    """Parse an uploaded WAV into mono 16-bit PCM at ``target_rate``."""
    try:
        with wave.open(io.BytesIO(data)) as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise TranscriptionError(f"not a readable WAV upload: {exc}") from exc
    if width != 2:
        raise TranscriptionError(f"expected 16-bit audio, got {width * 8}-bit")
    if channels > 1:
        frames = _mix_to_mono(frames, channels)
    utterance = Utterance(pcm=frames, sample_rate=rate)
    return utterance.resample(target_rate) if target_rate else utterance


def _mix_to_mono(pcm: bytes, channels: int) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % (2 * channels))])
    out = array.array("h", bytes(2 * (len(samples) // channels)))
    for i in range(len(out)):
        chunk = samples[i * channels : (i + 1) * channels]
        out[i] = int(sum(chunk) / channels)
    return out.tobytes()


class Transcriber(Protocol):
    name: str

    def transcribe(self, utterance: Utterance) -> str:
        """Return the recognized text, or "" when the audio held no speech."""


class NullTranscriber:
    """Returns nothing. Used by the tests and by ``--backend null``."""

    name = "null"

    def transcribe(self, utterance: Utterance) -> str:
        return ""


class FasterWhisperTranscriber:
    """Local Whisper inference via CTranslate2."""

    name = "faster-whisper"

    def __init__(self, config: TranscribeConfig) -> None:
        self.config = config
        self._model = None

    def load(self) -> None:
        """Load the model up front, so the first question is not slow."""
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            raise TranscriptionError(
                "faster-whisper is not installed. Run 'pip install faster-whisper', "
                "or start with --backend openai."
            ) from exc
        device = self.config.device
        if device == "auto":
            device = "cuda" if _has_cuda() else "cpu"
        log.info("loading whisper model %s on %s", self.config.model, device)
        self._model = WhisperModel(
            self.config.model, device=device, compute_type=self.config.compute_type
        )

    def transcribe(self, utterance: Utterance) -> str:
        self.load()
        import numpy as np  # type: ignore

        audio = np.frombuffer(utterance.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(  # type: ignore[union-attr]
            audio,
            language=self.config.language,
            beam_size=self.config.beam_size,
            initial_prompt=self.config.initial_prompt,
            vad_filter=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class OpenAITranscriber:
    """Any OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    name = "openai"

    def __init__(self, config: TranscribeConfig) -> None:
        self.config = config

    def load(self) -> None:
        self._api_key()

    def _api_key(self) -> str:
        key = os.environ.get(self.config.api_key_env, "").strip()
        if not key:
            raise TranscriptionError(
                f"${self.config.api_key_env} is not set; needed by the 'openai' backend."
            )
        return key

    def transcribe(self, utterance: Utterance) -> str:
        body, content_type = _multipart(
            {"model": self.config.api_model, "response_format": "json"},
            filename="speech.wav",
            file_bytes=utterance.to_wav(),
        )
        request = urllib.request.Request(
            f"{self.config.api_base.rstrip('/')}/audio/transcriptions",
            data=body,
            headers={"Authorization": f"Bearer {self._api_key()}", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.api_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(f"transcription API returned {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise TranscriptionError(f"transcription API unreachable: {exc.reason}") from exc
        return str(payload.get("text", "")).strip()


def _multipart(fields: dict[str, str], *, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder, so requests stays out of the deps."""
    boundary = f"----micclaude{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _has_cuda() -> bool:
    try:
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def build_transcriber(config: TranscribeConfig) -> Transcriber:
    backend = config.backend.lower().replace("_", "-")
    if backend in ("faster-whisper", "whisper", "local"):
        return FasterWhisperTranscriber(config)
    if backend in ("openai", "api", "http"):
        return OpenAITranscriber(config)
    if backend in ("null", "none", "off"):
        return NullTranscriber()
    raise TranscriptionError(f"unknown transcription backend: {config.backend!r}")
