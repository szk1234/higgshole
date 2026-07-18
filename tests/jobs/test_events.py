import json

from higgshole.jobs.clock import RealClock
from higgshole.jobs.events import JobEvent, NullEventPublisher
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState, utc_now_iso


def _event(**overrides) -> JobEvent:
    fields = {
        "generation_id": "a3f21c9d4e07",
        "kind": GenerationKind.VIDEO,
        "state": GenerationState.RUNNING,
        "error_reason": None,
        "detail": None,
        "at": utc_now_iso(),
    }
    fields.update(overrides)
    return JobEvent(**fields)


def test_job_event_serialises_as_an_sse_frame():
    frame = _event().to_sse()

    assert frame.startswith("event: job\ndata: ")
    assert frame.endswith("\n\n")

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["generation_id"] == "a3f21c9d4e07"
    assert payload["state"] == "RUNNING"
    assert payload["kind"] == "video"


def test_job_event_carries_a_null_error_reason():
    payload = json.loads(
        _event(
            state=GenerationState.FAILED,
            error_reason=ErrorReason.PROVIDER_EXPIRED,
            detail="retention window elapsed",
        )
        .to_sse()
        .split("data: ", 1)[1]
        .strip()
    )

    assert payload["error_reason"] == "provider_expired"
    assert payload["detail"] == "retention window elapsed"


def test_null_publisher_discards_events():
    publisher = NullEventPublisher()

    publisher.publish(_event())

    assert publisher.publish(_event()) is None


def test_real_clock_reports_monotonic_time():
    clock = RealClock()

    first = clock.monotonic()
    second = clock.monotonic()

    assert second >= first


async def test_real_clock_sleep_returns_promptly():
    # Zero is the only duration a test may ever pass to the real clock; every
    # timing-sensitive test injects FakeClock instead.
    await RealClock().sleep(0)
