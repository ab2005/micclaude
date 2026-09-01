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
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

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
        """Resample, averaging over each output sample's span when going down.

        Point sampling is aliasing: dropping 48 kHz to 16 kHz by picking every
        third sample folds everything above 8 kHz back into the speech band,
        and sibilants land on top of the vowels as noise. Averaging is a crude
        low-pass, but it is the difference between speech and mush.
        """
        if rate == self.sample_rate or not self.pcm:
            return self
        samples = array.array("h")
        samples.frombytes(self.pcm)
        if not samples:
            return self
        ratio = self.sample_rate / rate
        count = max(1, int(len(samples) / ratio))
        out = array.array("h", bytes(2 * count))
        if ratio <= 1:  # upsampling: interpolate
            for i in range(count):
                position = i * ratio
                left = int(position)
                right = min(left + 1, len(samples) - 1)
                weight = position - left
                out[i] = int(samples[left] * (1 - weight) + samples[right] * weight)
            return Utterance(pcm=out.tobytes(), sample_rate=rate)

        span = max(1, int(ratio))
        for i in range(count):
            start = int(i * ratio)
            stop = min(start + span, len(samples))
            out[i] = int(sum(samples[start:stop]) / (stop - start))
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
        self._previous = ""
        """The last thing heard, fed back as context for the next phrase."""

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

    def prompt_for(self, previous: str) -> str | None:
        """What to bias this phrase with: the configured hint, then context.

        A fragment of a few seconds is much easier to get right when the model
        knows what came just before. condition_on_previous_text does this too,
        and unboundedly -- one bad guess then feeds the next. This is capped
        and thrown away as soon as it stops helping.
        """
        parts = [self.config.initial_prompt or ""]
        words = previous.split()
        if self.config.context_words and words:
            parts.append(" ".join(words[-self.config.context_words :]))
        prompt = " ".join(part for part in parts if part).strip()
        return prompt or None

    def transcribe(self, utterance: Utterance) -> str:
        self.load()
        import numpy as np  # type: ignore

        save_debug_audio(utterance, self.config.debug_audio_dir)
        audio = np.frombuffer(utterance.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        prompt = self.prompt_for(self._previous)
        segments, _info = self._model.transcribe(  # type: ignore[union-attr]
            audio,
            language=self.config.language,
            beam_size=self.config.beam_size,
            initial_prompt=prompt,
            condition_on_previous_text=self.config.condition_on_previous_text,
            temperature=self.config.temperature,
            vad_filter=self.config.vad_filter,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return self._clean(text, prompt)

    def _clean(self, text: str, prompt: str | None) -> str:
        """Drop what is not speech, and remember what is."""
        if is_phantom(text, self.config.drop_phrases):
            log.info("dropping a phantom phrase from silence: %r", text)
            return ""
        if is_prompt_echo(text, prompt) or is_prompt_echo(text, self.config.initial_prompt):
            log.info("dropping our own prompt handed back as speech: %r", text)
            self._previous = ""  # the context is not helping; start over
            return ""
        if text:
            self._previous = text
        return text


class WhisperCppTranscriber:
    """whisper.cpp through its own HTTP server.

    On Apple silicon this is the fast path: whisper.cpp uses Metal, which
    CTranslate2 -- and so faster-whisper -- does not. On a passively cooled
    machine that is the difference between keeping up and falling behind.

    We speak to ``whisper-server`` rather than the one-shot CLI because the CLI
    reloads the model on every invocation; at a few utterances a minute that
    would dominate the cost. By default the server is ours to start and stop;
    point ``server_url`` at your own and set ``autostart = false`` to share one.
    """

    name = "whisper.cpp"

    def __init__(self, config: TranscribeConfig) -> None:
        self.config = config
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ model

    def model_path(self) -> Path:
        """Resolve ``model`` to a ggml file: a path as given, a name looked up."""
        raw = self.config.model.strip()
        candidate = Path(raw).expanduser()
        if candidate.suffix == ".bin" or "/" in raw:
            return candidate
        name = raw if raw.startswith("ggml-") else f"ggml-{raw}"
        return (Path(self.config.model_dir).expanduser() / name).with_suffix(".bin")

    def server_argv(self, model: Path, port: int) -> list[str]:
        argv = [self.config.server_binary, "-m", str(model), "--port", str(port)]
        if self.config.language:
            argv += ["-l", self.config.language]
        return argv + list(self.config.server_args)

    # ----------------------------------------------------------------- server

    def load(self) -> None:
        """Make sure something is listening, starting it if that is our job."""
        with self._lock:
            if self._reachable():
                return
            if not self.config.autostart:
                raise TranscriptionError(
                    f"nothing is listening on {self.config.server_url}. Start whisper-server "
                    "there, or set transcribe.autostart to let micclaude start it."
                )
            self._start()

    def _reachable(self) -> bool:
        try:
            urllib.request.urlopen(self.config.server_url, timeout=1.0)
        except urllib.error.HTTPError:
            return True  # answered, even if it dislikes the path
        except (urllib.error.URLError, OSError):
            return False
        return True

    def _start(self) -> None:
        if shutil.which(self.config.server_binary) is None:
            raise TranscriptionError(
                f"'{self.config.server_binary}' is not on PATH. On a Mac: "
                "'brew install whisper-cpp'. Elsewhere, build whisper.cpp and put "
                "its server on PATH, or set transcribe.server_binary."
            )
        model = self.model_path()
        if not model.is_file():
            raise TranscriptionError(
                f"no speech model at {model}. Download one, for example:\n"
                f"  mkdir -p {model.parent} && curl -L -o {model} \\\n"
                "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                f"{model.name}"
            )
        port = urllib.parse.urlsplit(self.config.server_url).port or 8181
        argv = self.server_argv(model, port)
        log.info("starting %s", " ".join(argv))
        try:
            self._process = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
        except OSError as exc:
            raise TranscriptionError(f"could not start {self.config.server_binary}: {exc}") from exc
        self._await_ready()

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                tail = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
                self._process = None
                raise TranscriptionError(f"{self.config.server_binary} exited at startup: {tail}")
            if self._reachable():
                return
            time.sleep(0.25)
        self.stop()
        raise TranscriptionError(
            f"{self.config.server_binary} did not come up within "
            f"{self.config.startup_timeout:.0f}s"
        )

    def stop(self) -> None:
        """Stop the server, if it is one we started."""
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            process.kill()

    # ------------------------------------------------------------ transcribe

    def transcribe(self, utterance: Utterance) -> str:
        self.load()
        save_debug_audio(utterance, self.config.debug_audio_dir)
        fields = {"response_format": "json", "temperature": "0"}
        if self.config.language:
            fields["language"] = self.config.language
        body, content_type = _multipart(fields, filename="speech.wav", file_bytes=utterance.to_wav())
        request = urllib.request.Request(
            self.config.server_url.rstrip("/") + "/inference",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.api_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(f"whisper-server returned {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TranscriptionError(f"whisper-server is unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TranscriptionError(f"whisper-server answered with something odd: {exc}") from exc

        text = str(payload.get("text") or "").strip()
        if is_phantom(text, self.config.drop_phrases):
            log.info("dropping a phantom phrase from silence: %r", text)
            return ""
        if is_prompt_echo(text, self.config.initial_prompt):
            log.info("dropping our own prompt handed back as speech: %r", text)
            return ""
        return text


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


def is_phantom(text: str, phrases: Sequence[str]) -> bool:
    """Is this the whole utterance, and one of Whisper's subtitle ghosts?

    Trained partly on subtitles, Whisper fills silence with the credits it
    learned there. The match is on the entire utterance: these words are only
    suspicious when they are all that came back.
    """
    cleaned = re.sub(r"[\s\u00a0]+", " ", (text or "").strip().lower()).strip(" .!?,-—…")
    if not cleaned:
        return False
    return any(cleaned == re.sub(r"\s+", " ", phrase.strip().lower()).strip(" .!?,-—…") for phrase in phrases)


def save_debug_audio(utterance: Utterance, directory: str | None) -> Path | None:
    """Keep the audio of one utterance, so a person can hear what the model heard.

    Off unless asked for -- it is the one thing here that stores sound. When
    recognition is bad this is the only way to tell a bad room from a bad
    model: play the file, and if you cannot understand it either, the fix is
    not in software.
    """
    if not directory:
        return None
    target = Path(directory).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = target / f"{time.strftime('%H%M%S')}-{int(utterance.duration_ms)}ms.wav"
        path.write_bytes(utterance.to_wav())
        path.chmod(0o600)
    except OSError as exc:  # pragma: no cover - disk trouble
        log.warning("cannot save debug audio: %s", exc)
        return None
    return path


def is_prompt_echo(text: str, prompt: str | None) -> bool:
    """Did Whisper hand our own initial_prompt back as the transcript?

    Given a biasing prompt and audio it cannot make sense of, Whisper will
    happily return the prompt. It looks like speech, it is not, and no list of
    known phantoms can catch it -- but comparing against what we sent can.
    """
    if not prompt:
        return False
    cleaned = _flatten(text)
    return bool(cleaned) and cleaned == _flatten(prompt)


def _flatten(text: str) -> str:
    return re.sub(r"[^\w\s]", "", re.sub(r"\s+", " ", (text or "").strip().lower())).strip()


def _has_cuda() -> bool:
    """Is there a GPU CTranslate2 can use? Absent one, faster-whisper runs on CPU."""
    try:
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def build_transcriber(config: TranscribeConfig) -> Transcriber:
    backend = config.backend.lower().replace("_", "-")
    if backend in ("faster-whisper", "whisper", "local"):
        return FasterWhisperTranscriber(config)
    if backend in ("whisper.cpp", "whispercpp", "whisper-cpp", "metal"):
        return WhisperCppTranscriber(config)
    if backend in ("openai", "api", "http"):
        return OpenAITranscriber(config)
    if backend in ("null", "none", "off"):
        return NullTranscriber()
    raise TranscriptionError(f"unknown transcription backend: {config.backend!r}")
