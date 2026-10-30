# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Server-Sent Events for the UI (C11-M1 README 7.6, C12-M1 README 7.6).

A module keeps one `Broker`. `broker.publish(event, data, meta)` may be called from any thread
(event relay, worker); `meta` is used only to decide who sees the event and is never sent.
`response(...)` returns the `text/event-stream` answer of one operation:

- the token is checked by the route dependency before the stream opens (header only);
- `retry: 3000` first; without `Last-Event-ID` the `initial()` events (the current state);
- with `Last-Event-ID` the kept events after it, or `on_gap()` if this instance no longer has it;
- a comment `: keep-alive` every `SSE_HEARTBEAT_SECONDS`; the stream ends when `exp` of the token
  has passed or the client goes away.

The `id` of an event is `<instance>-<number>`; clients send it back unchanged.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import StreamingResponse

Message = tuple[str, dict]   # (event name, data)


@dataclass(frozen=True)
class Entry:
    seq: int
    event: str
    data: dict
    meta: dict


class Broker:
    # MINIMAL: replay buffer in memory per instance; a reconnect to another instance or after a
    # restart gets `on_gap()` (C12-M1 open question 8). Upgrade: a table or the outbox as buffer.
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._epoch = uuid.uuid4().hex[:12]
            self._seq = 0
            self._buffer: collections.deque[Entry] = collections.deque()
            waiters = list(self._waiters)
        self._wake(waiters)

    def event_id(self, seq: int) -> str:
        return f"{self._epoch}-{seq}"

    def last_id(self) -> str:
        with self._lock:
            return self.event_id(self._seq)

    def publish(self, event: str, data: dict, meta: dict | None = None) -> str:
        size = max(1, int(os.environ.get("SSE_REPLAY_SIZE", 1000)))
        with self._lock:
            self._seq += 1
            self._buffer.append(Entry(self._seq, event, data, meta or {}))
            while len(self._buffer) > size:
                self._buffer.popleft()
            eid, waiters = self.event_id(self._seq), list(self._waiters)
        self._wake(waiters)
        return eid

    def after(self, last_id: str) -> tuple[str, list[Entry] | None]:
        """(id of the newest event, kept entries after `last_id`); None if `last_id` is not kept."""
        epoch, _, num = last_id.partition("-")
        with self._lock:
            newest = self.event_id(self._seq)
            if epoch != self._epoch or not num.isdigit() or int(num) > self._seq:
                return newest, None
            first = self._buffer[0].seq if self._buffer else self._seq + 1
            if int(num) < first - 1:
                return newest, None
            return newest, [e for e in self._buffer if e.seq > int(num)]

    @staticmethod
    def _wake(waiters) -> None:
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:   # loop already closed
                pass

    def _add(self, waiter) -> None:
        with self._lock:
            self._waiters.add(waiter)

    def _remove(self, waiter) -> None:
        with self._lock:
            self._waiters.discard(waiter)


def _format(eid: str, event: str, data: dict) -> str:
    return f"id: {eid}\nevent: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _stream(broker: Broker, request: Request, visible: Callable[[dict], bool],
                  initial: Callable[[], list[Message]] | None, on_gap: Callable[[], list[Message]],
                  last_event_id: str | None, expires_at: float) -> AsyncIterator[str]:
    flag = asyncio.Event()
    waiter = (asyncio.get_running_loop(), flag)
    broker._add(waiter)
    heartbeat = float(os.environ.get("SSE_HEARTBEAT_SECONDS", 15))
    try:
        yield "retry: 3000\n\n"
        cursor = last_event_id or broker.last_id()   # taken before the snapshot: nothing is lost
        if not last_event_id and initial:
            for event, data in initial():
                yield _format(cursor, event, data)
        while True:
            flag.clear()   # before reading: a publish from now on wakes the wait below
            newest, entries = broker.after(cursor)
            if entries is None:
                for event, data in on_gap():
                    yield _format(newest, event, data)
            else:
                for e in entries:
                    if visible(e.meta):
                        yield _format(broker.event_id(e.seq), e.event, e.data)
            cursor = newest
            remaining = expires_at - time.time()
            if remaining <= 0 or await request.is_disconnected():
                return   # token expired: the UI reconnects with a new token and Last-Event-ID
            try:
                await asyncio.wait_for(flag.wait(), timeout=min(heartbeat, remaining))
            except TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        broker._remove(waiter)


def response(broker: Broker, request: Request, claims: dict, last_event_id: str | None, *,
             visible: Callable[[dict], bool], initial: Callable[[], list[Message]] | None = None,
             on_gap: Callable[[], list[Message]] | None = None) -> StreamingResponse:
    """`text/event-stream` answer. `initial`: events at a start without `Last-Event-ID`;
    `on_gap`: events when the `Last-Event-ID` is not kept (default: `initial`)."""
    gap = on_gap or initial or (lambda: [])
    body = _stream(broker, request, visible, initial, gap, (last_event_id or "").strip()[:64] or None,
                   float(claims.get("exp", time.time())))
    return StreamingResponse(body, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
