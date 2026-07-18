import asyncio
import json

import pytest

from higgshole.jobs.events import JobEvent
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState
from higgshole.web import sse
from higgshole.web.sse import EventBus, event_stream


def _event(
    *,
    gen_id: str = "a3f21c9d4e07",
    state: GenerationState = GenerationState.RUNNING,
    error_reason: ErrorReason | None = None,
) -> JobEvent:
    return JobEvent(
        generation_id=gen_id,
        kind=GenerationKind.VIDEO,
        state=state,
        error_reason=error_reason,
        detail=None,
        at="2026-07-18T14:30:22.104883+00:00",
    )


def test_event_serialises_as_a_named_sse_frame():
    # The web layer re-exports the runners' event rather than redefining it,
    # so the two can never drift into merely duck-type compatibility.
    assert sse.JobEvent is JobEvent

    frame = _event().to_sse()

    assert frame.startswith("event: job\ndata: ")
    assert frame.endswith("\n\n")

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["generation_id"] == "a3f21c9d4e07"
    assert payload["state"] == "RUNNING"
    assert payload["kind"] == "video"


def test_the_frame_carries_the_machine_readable_failure_reason():
    frame = _event(
        state=GenerationState.FAILED, error_reason=ErrorReason.PROVIDER_EXPIRED
    ).to_sse()

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["error_reason"] == "provider_expired"


async def test_a_subscriber_receives_published_events():
    bus = EventBus()

    async with bus.subscribe() as events:
        bus.publish(_event(gen_id="000000000001"))
        received = await asyncio.wait_for(anext(events), timeout=1)

    assert received.generation_id == "000000000001"


async def test_every_subscriber_receives_the_same_event():
    bus = EventBus()

    async with bus.subscribe() as first, bus.subscribe() as second:
        assert bus.listener_count == 2
        bus.publish(_event())

        a = await asyncio.wait_for(anext(first), timeout=1)
        b = await asyncio.wait_for(anext(second), timeout=1)

    assert a == b


async def test_a_full_queue_drops_the_oldest_event():
    # A slow browser tab must never stall a job runner, so publish is
    # non-blocking and sheds the oldest event instead.
    bus = EventBus(max_queue=2)

    async with bus.subscribe() as events:
        bus.publish(_event(gen_id="000000000001"))
        bus.publish(_event(gen_id="000000000002"))
        bus.publish(_event(gen_id="000000000003"))

        first = await asyncio.wait_for(anext(events), timeout=1)
        second = await asyncio.wait_for(anext(events), timeout=1)

    assert [first.generation_id, second.generation_id] == [
        "000000000002",
        "000000000003",
    ]


async def test_leaving_the_subscription_removes_the_listener():
    bus = EventBus()

    async with bus.subscribe():
        assert bus.listener_count == 1

    assert bus.listener_count == 0
    bus.publish(_event())  # must not raise with no listeners


async def test_the_stream_emits_a_keepalive_comment_when_idle():
    # Proxies close idle connections; a comment line keeps the stream open
    # without inventing a job event.
    stream = event_stream(EventBus(), keepalive_seconds=0.01)

    try:
        assert await asyncio.wait_for(anext(stream), timeout=1) == ": keepalive\n\n"
    finally:
        await stream.aclose()


@pytest.mark.parametrize("state", [GenerationState.COMPLETE, GenerationState.FAILED])
async def test_the_stream_forwards_published_events(state):
    bus = EventBus()
    stream = event_stream(bus, keepalive_seconds=5)

    try:
        bus.publish(_event(state=state))
        # Give the generator a turn to register its subscription first.
        await asyncio.sleep(0)
        bus.publish(_event(state=state))
        frame = await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()

    assert f'"state": "{state.value}"' in frame
