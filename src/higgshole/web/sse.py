"""Live job status over Server-Sent Events.

Fan-out is in-process because the deployment runs exactly one uvicorn worker
(spec section 9); a broker would buy nothing and add an operational
dependency to a single-user application.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from higgshole.jobs.events import JobEvent

#: How long the stream waits before emitting a comment line. Proxies commonly
#: close a connection idle for 30-60s, so this stays comfortably below that.
KEEPALIVE_SECONDS: float = 15.0

#: Re-exported, never redefined. `JobEvent` belongs to `jobs/` because the
#: dependency direction is web -> jobs (spec section 4.1), and the runners
#: construct that class. A second frozen dataclass with the same field names
#: would be a different type, so every `isinstance` and `Protocol` check
#: against `EventPublisher` would quietly mislead.
__all__ = [
    "KEEPALIVE_SECONDS",
    "EventBus",
    "JobEvent",
    "event_stream",
    "router",
]


class EventBus:
    """In-process fan-out from the job runners to every open browser tab."""

    def __init__(self, *, max_queue: int = 100) -> None:
        self._max_queue = max_queue
        self._listeners: set[asyncio.Queue[JobEvent]] = set()

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def publish(self, event: JobEvent) -> None:
        """Deliver to every listener without ever blocking.

        A listener whose queue is full loses its oldest event. Back-pressure
        onto a job runner would let a stalled tab delay a paid generation,
        which is a far worse failure than a missing status line.
        """
        for queue in list(self._listeners):
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    break
            queue.put_nowait(event)

    def attach(self) -> asyncio.Queue[JobEvent]:
        """Register a listener queue synchronously.

        Separate from `subscribe` because `event_stream` has to register
        before its first iteration: an async generator's body does not run
        until `__anext__` is awaited, so a generator that subscribed inside
        itself would silently drop every event published between the moment
        the caller built the stream and the moment it first read from it.
        """
        queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=self._max_queue)
        self._listeners.add(queue)
        return queue

    def detach(self, queue: asyncio.Queue[JobEvent]) -> None:
        self._listeners.discard(queue)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[JobEvent]]:
        """Register a listener queue and remove it on exit."""
        queue = self.attach()

        async def _iterate() -> AsyncIterator[JobEvent]:
            while True:
                yield await queue.get()

        iterator = _iterate()
        try:
            yield iterator
        finally:
            self.detach(queue)
            await iterator.aclose()


def event_stream(
    bus: EventBus, *, keepalive_seconds: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    """Yield SSE text frames until the client disconnects.

    Kept separate from the route so the framing is testable without an
    application, a server, or a socket. The listener is registered here, in
    the synchronous call, rather than inside the generator; the generator's
    `finally` removes it again when the response task is cancelled on
    disconnect.
    """
    queue = bus.attach()

    async def _frames() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=keepalive_seconds
                    )
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield event.to_sse()
        finally:
            bus.detach(queue)

    return _frames()


router = APIRouter(prefix="/events", tags=["events"])


@router.get("/jobs")
async def stream_jobs(request: Request) -> StreamingResponse:
    """text/event-stream of every job state transition."""
    bus: EventBus = request.app.state.higgshole.events
    return StreamingResponse(
        event_stream(bus),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
