from decimal import Decimal

from higgshole.orclient.errors import (
    IndeterminateError,
    InsufficientCreditsError,
    ModerationError,
    RateLimitError,
)
from higgshole.store.db import ErrorReason, GenerationState, InputRole, LedgerKind
from tests.jobs.fakes import Harness, video_job


async def test_a_successful_image_run_reaches_complete(harness):
    harness.client.image_results.append(harness.image_result())

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert outcome.cost == Decimal("0.04")
    assert (harness.paths.root / outcome.file_path).exists()


async def test_the_state_sequence_is_pending_generating_writing_complete(harness):
    harness.client.image_results.append(harness.image_result())

    outcome = await harness.image_runner.run(harness.image_request())

    assert harness.events.states_for(outcome.generation_id) == [
        "PENDING",
        "GENERATING",
        "WRITING",
        "COMPLETE",
    ]


async def test_hard_validation_failure_is_rejected_before_dispatch(harness):
    outcome = await harness.image_runner.run(
        harness.image_request(params={"quality": "ultra"})
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION


async def test_a_rejected_request_never_calls_the_provider(harness):
    await harness.image_runner.run(harness.image_request(model="nobody/nothing"))

    assert harness.client.calls == []


async def test_cap_rejection_maps_to_cap_exceeded(tmp_path, stub_media):
    # Cap of 0.01 with a pessimistic ceiling of 2.00 cannot admit anything.
    harness = Harness(tmp_path, daily_cap_usd=Decimal("0.01"))
    try:
        outcome = await harness.image_runner.run(
            harness.image_request(params={"quality": "high"})
        )

        assert outcome.state is GenerationState.REJECTED
        assert outcome.error_reason is ErrorReason.CAP_EXCEEDED
        assert harness.client.calls == []
    finally:
        harness.db.close()


async def test_in_flight_rejection_maps_to_in_flight_limit(tmp_path, stub_media):
    harness = Harness(tmp_path, max_in_flight=1)
    try:
        # Occupy the only slot with a video row that never terminates.
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(50)
        )
        await harness.video_runner.submit(harness.video_request())

        outcome = await harness.image_runner.run(harness.image_request())

        assert outcome.state is GenerationState.REJECTED
        assert outcome.error_reason is ErrorReason.IN_FLIGHT_LIMIT
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_indeterminate_error_fails_without_retry(harness):
    # POST /images is synchronous and non-idempotent, so a retry risks a
    # second charge (spec section 4.4).
    harness.client.image_results.append(IndeterminateError("connection reset"))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.INDETERMINATE
    assert harness.client.call_names().count("generate_image") == 1


async def test_a_rate_limit_is_retried_before_dispatch(harness):
    harness.client.image_results.extend(
        [RateLimitError("slow down", status_code=429), harness.image_result()]
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert harness.client.call_names().count("generate_image") == 2
    assert harness.clock.slept  # backoff went through the injected clock


async def test_a_moderation_refusal_maps_to_moderation(harness):
    harness.client.image_results.append(
        ModerationError("Content policy violation", status_code=400)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.error_reason is ErrorReason.MODERATION
    assert "policy" in outcome.error_detail.lower()


async def test_insufficient_credits_maps_to_its_own_reason(harness):
    # Surfaced distinctly from the local cap so the operator knows which
    # guard tripped (spec section 10).
    harness.client.image_results.append(
        InsufficientCreditsError("credit limit reached", status_code=402)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.error_reason is ErrorReason.INSUFFICIENT_CREDITS


async def test_a_null_cost_leaves_the_reservation_standing(harness):
    # Spec section 3.4: the reservation stands as the recorded charge and the
    # day is marked a lower bound. Zero is never recorded as the charge.
    harness.client.image_results.append(harness.image_result(cost=None))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert outcome.cost is None

    rows = harness.db.list_ledger_for_generation(outcome.generation_id)
    kinds = [row.kind for row in rows]
    assert LedgerKind.RESERVATION in kinds
    assert LedgerKind.REVERSAL not in kinds
    assert harness.ledger_total(outcome.generation_id) > Decimal("0")
    assert any(row.cost_known is False for row in rows)


async def test_input_references_are_sent_as_data_uris(harness):
    asset_id = harness.upload("ref.png")
    harness.client.image_results.append(harness.image_result())

    await harness.image_runner.run(
        harness.image_request(inputs=((asset_id, InputRole.INPUT_REFERENCE),))
    )

    sent = harness.client.last_call("generate_image")["input_references"]
    assert len(sent) == 1
    assert sent[0].startswith("data:image/png;base64,")
