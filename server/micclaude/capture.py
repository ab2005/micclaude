"""Microphone capture and utterance segmentation for the recorder process.

This mirrors `web/js/segmenter.js` deliberately: the browser and the recorder
must cut speech at the same places, or the same sentence would be transcribed
differently depending on who heard it. `test_capture.py` runs both over the
same synthetic frames and compares the boundaries.

:class:`Segmenter` touches no audio library, so it is testable anywhere.
"""

from __future__ import annotations

import array
import io
import logging
import math
import queue
import wave
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Iterator

from .config import AudioConfig

log = logging.getLogger(__name__)


@dataclass
class Utterance:
    """One detected phrase of 16-bit mono PCM."""

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


def rms(frame: bytes) -> float:
    """Root-mean-square level of an int16 frame, normalized to 0..1."""
    if not frame:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples)) / 32768.0


class Segmenter:
    """Turns a stream of fixed-size frames into whole phrases."""

    def __init__(self, config: AudioConfig | None = None) -> None:
        self.config = config or AudioConfig()
        self.frame_samples = round(self.config.sample_rate * self.config.frame_ms / 1000)
        self.preroll_frames = max(1, self.config.preroll_ms // self.config.frame_ms)
        self.silence_frames = max(1, self.config.silence_ms // self.config.frame_ms)
        self.max_frames = max(1, self.config.max_utterance_ms // self.config.frame_ms)
        self.level = 0.0
        self.reset()

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2

    @property
    def is_speaking(self) -> bool:
        return self._triggered

    def reset(self) -> None:
        self._preroll: deque[bytes] = deque(maxlen=self.preroll_frames)
        self._voiced: list[bytes] = []
        self._speech_run = 0
        self._silence_run = 0
        self._triggered = False
        self.level = 0.0

    def push(self, frame: bytes) -> Utterance | None:
        """Feed one frame; returns an utterance when one just completed."""
        self.level = rms(frame)
        speech = self.level >= self.config.energy_threshold

        if not self._triggered:
            self._preroll.append(frame)
            self._speech_run = self._speech_run + 1 if speech else 0
            if self._speech_run >= self.config.start_frames:
                self._triggered = True
                self._voiced = list(self._preroll)
                self._preroll.clear()
                self._silence_run = 0
            return None

        self._voiced.append(frame)
        self._silence_run = 0 if speech else self._silence_run + 1
        if self._silence_run >= self.silence_frames or len(self._voiced) >= self.max_frames:
            return self._close()
        return None

    def flush(self) -> Utterance | None:
        """Close any phrase still in progress, e.g. when capture stops."""
        return self._close() if self._triggered else None

    def _close(self) -> Utterance | None:
        pcm = b"".join(self._voiced)
        self.reset()
        utterance = Utterance(pcm=pcm, sample_rate=self.config.sample_rate)
        if utterance.duration_ms < self.config.min_utterance_ms:
            log.debug("dropping a %.0fms fragment", utterance.duration_ms)
            return None
        return utterance

    def feed(self, frames: Iterable[bytes]) -> Iterator[Utterance]:
        """Convenience wrapper over :meth:`push` for offline sources."""
        for frame in frames:
            utterance = self.push(frame)
            if utterance is not None:
                yield utterance
        tail = self.flush()
        if tail is not None:
            yield tail


class Microphone:
    """Yields utterances from an input device.

    sounddevice delivers frames on its own thread; they cross into ours through
    a bounded queue, so a slow transcriber drops audio instead of growing
    memory without limit.
    """

    def __init__(self, config: AudioConfig, device: int | str | None = None, max_queued: int = 400) -> None:
        self.config = config
        self.device = device
        self.segmenter = Segmenter(config)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=max_queued)
        self._stream = None
        self.overflows = 0

    def start(self) -> None:
        try:
            import sounddevice  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on the machine
            raise RuntimeError(
                "sounddevice is required to record. Install it with "
                "'pip install sounddevice' (it needs PortAudio)."
            ) from exc

        def callback(indata, frames, time_info, status):  # pragma: no cover - audio thread
            if status:
                log.debug("audio status: %s", status)
            try:
                self._queue.put_nowait(bytes(indata))
            except queue.Full:
                self.overflows += 1

        self._stream = sounddevice.RawInputStream(
            samplerate=self.config.sample_rate,
            blocksize=self.segmenter.frame_samples,
            device=self.device,
            dtype="int16",
            channels=1,
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._queue.put(None)

    def utterances(self) -> Iterator[Utterance]:
        while True:
            frame = self._queue.get()
            if frame is None:
                break
            utterance = self.segmenter.push(frame)
            if utterance is not None:
                yield utterance


def list_devices() -> str:
    """Human-readable table of input devices, for ``--list-devices``."""
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        return f"sounddevice is not installed ({exc})."
    lines = ["index  channels  name"]
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) < 1:
            continue
        lines.append(f"{index:>5}  {info['max_input_channels']:>8}  {info['name']}")
    return "\n".join(lines)
