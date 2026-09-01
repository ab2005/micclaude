"""Pushing events from the server to whatever pages are open.

Recognized speech no longer has to come from the browser: a separate recorder
process can post text in. Any page that is open needs to see it, so the server
keeps a fan-out of subscriber queues and pushes an event to each.

Subscribers are bounded. A page that stops reading (a laptop that went to
sleep, a tab throttled in the background) drops its oldest events rather than
growing without limit, and a page that goes away is dropped entirely.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Iterator

log = logging.getLogger(__name__)

QUEUE_LIMIT = 200


class Broadcaster:
    """Fan-out of server events to subscribed pages."""

    def __init__(self, queue_limit: int = QUEUE_LIMIT) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._queue_limit = queue_limit

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, name: str, data: dict[str, Any]) -> int:
        """Send one event to every subscriber. Returns how many got it."""
        event = (name, data)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # Drop the oldest rather than the newest: a page catching up
                # cares about the last thing said, not the first.
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):  # pragma: no cover - racy
                    log.debug("subscriber queue is wedged; dropping an event")
        return len(subscribers)

    def subscribe(self) -> "Subscription":
        subscriber: queue.Queue = queue.Queue(maxsize=self._queue_limit)
        with self._lock:
            self._subscribers.append(subscriber)
        return Subscription(self, subscriber)

    def _unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def close(self) -> None:
        """Wake every subscriber so its stream ends."""
        with self._lock:
            subscribers, self._subscribers = self._subscribers, []
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:  # pragma: no cover - racy
                pass


class Subscription:
    """One page's view of the event stream."""

    def __init__(self, broadcaster: Broadcaster, subscriber: queue.Queue) -> None:
        self._broadcaster = broadcaster
        self._queue = subscriber

    def events(self, keepalive: float = 15.0) -> Iterator[tuple[str, dict[str, Any]] | None]:
        """Yield events, or None when it is time to send a keepalive.

        Proxies and sleeping laptops quietly drop an idle connection, and the
        page only learns about it when nothing arrives for an hour. A periodic
        comment keeps the stream honest.
        """
        try:
            while True:
                try:
                    event = self._queue.get(timeout=keepalive)
                except queue.Empty:
                    yield None
                    continue
                if event is None:
                    return
                yield event
        finally:
            self.close()

    def close(self) -> None:
        self._broadcaster._unsubscribe(self._queue)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
