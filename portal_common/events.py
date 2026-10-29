# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Event relay of an installation: stand-in for "outbox per module + relay" (FRAMEWORK 1).

`publish()` only puts the event into the outbox and returns at once; a relay thread delivers it
to every subscriber. A request therefore never runs another module's reaction (no synchronous
bus, no deadlock), and an exception in one subscriber is logged in
`failures` without stopping delivery to the others.

Without `start()` (unit tests) nothing is delivered until `drain()` is called.
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import threading
import traceback
import uuid
from collections.abc import Callable

log = logging.getLogger("portal.events")
FORBIDDEN_KEYS = ("secret", "password", "apikey", "token")


class Relay:
    def __init__(self) -> None:
        self._queue: collections.deque[dict] = collections.deque()
        self._cv = threading.Condition()
        self._subscribers: list[tuple[str, Callable[[dict], None]]] = []
        self._busy = False
        self._thread: threading.Thread | None = None
        self._stop = False
        self.log: list[dict] = []
        self.failures: list[dict] = []

    def subscribe(self, name: str, handler: Callable[[dict], None]) -> None:
        self._subscribers.append((name, handler))

    def clear_subscribers(self) -> None:
        self._subscribers.clear()

    def publish(self, event: dict) -> dict:
        if not event.get("eventType"):
            raise ValueError("event without eventType")
        bad = [k for k in event if any(f in k.lower() for f in FORBIDDEN_KEYS)]
        if bad:
            raise ValueError(f"events never carry secrets (FRAMEWORK 4.6): {bad}")
        ev = {"eventId": str(uuid.uuid4()), "occurredAt": dt.datetime.now(dt.UTC).isoformat(), **event}
        with self._cv:
            self.log.append(ev)
            self._queue.append(ev)
            self._cv.notify_all()
        return ev

    def _deliver(self, ev: dict) -> None:
        for name, handler in list(self._subscribers):
            try:
                handler(ev)
            except Exception as exc:  # noqa: BLE001 - one subscriber must not stop the others
                self.failures.append({"subscriber": name, "eventId": ev["eventId"], "eventType": ev["eventType"],
                                      "error": repr(exc)})
                log.error("subscriber %s failed on %s: %s", name, ev["eventType"], "".join(traceback.format_exception(exc)))

    def _next(self, block: bool) -> dict | None:
        with self._cv:
            while block and not self._queue and not self._stop:
                self._cv.wait()
            if not self._queue:
                return None
            self._busy = True
            return self._queue.popleft()

    def _done(self) -> None:
        with self._cv:
            self._busy = False
            self._cv.notify_all()

    def drain(self) -> None:
        """Delivers everything queued, in the calling thread (for tests without the relay thread)."""
        while (ev := self._next(block=False)) is not None:
            try:
                self._deliver(ev)
            finally:
                self._done()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False

        def loop() -> None:
            while not self._stop:
                ev = self._next(block=True)
                if ev is None:
                    continue
                try:
                    self._deliver(ev)
                finally:
                    self._done()

        self._thread = threading.Thread(target=loop, name="event-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """True when the outbox is empty and no delivery is running."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._queue and not self._busy, timeout)

    def reset(self) -> None:
        with self._cv:
            self._queue.clear()
            self.log.clear()
            self.failures.clear()


RELAY = Relay()
publish = RELAY.publish
subscribe = RELAY.subscribe
