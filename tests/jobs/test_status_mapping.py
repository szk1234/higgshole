import pytest

from higgshole.jobs.runner import (
    GenerationRequest,
    RetryPolicy,
    map_provider_status,
)
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    [
        ("pending", GenerationState.RUNNING, None),
        ("in_progress", GenerationState.RUNNING, None),
        ("completed", GenerationState.DOWNLOADING, None),
        ("failed", GenerationState.FAILED, ErrorReason.PROVIDER_FAILED),
        ("cancelled", GenerationState.FAILED, ErrorReason.PROVIDER_CANCELLED),
        ("expired", GenerationState.FAILED, ErrorReason.PROVIDER_EXPIRED),
    ],
)
def test_every_documented_status_maps_as_the_specification_requires(status, state, reason):
    assert map_provider_status(status) == (state, reason)


def test_unrecognised_status_keeps_polling():
    # Spec section 2.4: over-polling is bounded by the wall-clock ceiling and
    # self-corrects; treating a live job as terminal loses a paid generation.
    assert map_provider_status("reticulating") == (GenerationState.RUNNING, None)
    assert map_provider_status("") == (GenerationState.RUNNING, None)


def test_every_terminal_provider_status_has_a_reason():
    for status in ("failed", "cancelled", "expired"):
        state, reason = map_provider_status(status)
        assert state is GenerationState.FAILED
        assert reason is not None


def test_retry_delay_grows_and_is_capped():
    policy = RetryPolicy(max_retries=8, base_delay_s=1.0, max_delay_s=30.0)

    # Full jitter means each delay is a sample from [0, ceiling], so the
    # ceiling is what is asserted, not the sample.
    assert all(policy.delay_for(attempt) <= 30.0 for attempt in range(10))
    assert max(policy.delay_for(0) for _ in range(200)) <= 1.0
    assert max(policy.delay_for(3) for _ in range(200)) <= 8.0


def test_retry_delay_is_never_negative():
    policy = RetryPolicy()

    assert all(policy.delay_for(attempt) >= 0.0 for attempt in range(-2, 6))


def test_generation_request_defaults_to_no_inputs():
    request = GenerationRequest(
        kind=GenerationKind.IMAGE,
        project_id="p1",
        project_slug="unsorted",
        model="a/b",
        prompt="a cat",
        params={},
    )

    assert request.inputs == ()
