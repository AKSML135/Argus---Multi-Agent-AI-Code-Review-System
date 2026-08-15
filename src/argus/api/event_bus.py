"""In-process pub/sub for review progress events.

The graph must only ever be driven by ONE coroutine per thread_id
(`_run_review`'s single `astream_events()` call). The SSE `/stream`
endpoint must NOT call `astream`/`astream_events` itself — doing so
starts a second, concurrent execution of the same LangGraph thread,
which races the background run and corrupts checkpoints.

Instead, `_run_review` publishes each event here, and `/stream`
subscribes and just relays whatever comes through — no graph access
at all.
"""

from __future__ import annotations

import asyncio


class _ReviewBroadcaster:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._buffer: list[dict] = []
        self._finished = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for event in self._buffer:
            q.put_nowait(event)
        if self._finished:
            q.put_nowait(None)  # sentinel: stream is over
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        self._buffer.append(event)
        for q in self._subscribers:
            q.put_nowait(event)

    def finish(self) -> None:
        self._finished = True
        for q in self._subscribers:
            q.put_nowait(None)


_broadcasters: dict[str, _ReviewBroadcaster] = {}


def get_broadcaster(review_id: str) -> _ReviewBroadcaster:
    if review_id not in _broadcasters:
        _broadcasters[review_id] = _ReviewBroadcaster()
    return _broadcasters[review_id]


def drop_broadcaster(review_id: str) -> None:
    _broadcasters.pop(review_id, None)