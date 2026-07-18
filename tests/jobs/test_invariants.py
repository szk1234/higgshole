import asyncio
from decimal import Decimal
from pathlib import Path

import higgshole.jobs as jobs_package
from higgshole.orclient.errors import ProviderError
from higgshole.store.db import ErrorReason, GenerationState, LedgerKind
from tests.jobs.fakes import Harness, video_job


async def test_concurrent_submissions_cannot_exceed_the_daily_cap(tmp_path, stub_media):
    """Spec section 3.3: without a serialized gate, ten submissions in one
    second would each observe the same remaining balance."""
    harness = Harness(tmp_path, daily_cap_usd=Decimal("3.00"), max_in_flight=10)
    try:
        # The video model is token-priced, so no exact estimate exists and each
        # job reserves the pessimistic ceiling of 2.00. A 3.00 cap admits one.
        for index in range(3):
            harness.client.submit_results.append(video_job(f"job-{index}", "pending"))
        harness.client.poll_results.extend(
            video_job("job-0", "in_progress") for _ in range(200)
        )

        outcomes = await asyncio.gather(
            *(harness.video_runner.submit(harness.video_request()) for _ in range(3))
        )

        submitted = [o for o in outcomes if o.state is GenerationState.SUBMITTED]
        rejected = [o for o in outcomes if o.state is GenerationState.REJECTED]

        assert len(submitted) == 1
        assert len(rejected) == 2
        assert all(o.error_reason is ErrorReason.CAP_EXCEEDED for o in rejected)

        # Total reserved never exceeded the cap.
        outstanding = harness.ledger.outstanding_reservations()
        assert outstanding <= Decimal("3.00")
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_concurrent_submissions_respect_the_in_flight_ceiling(
    tmp_path, stub_media
):
    harness = Harness(tmp_path, daily_cap_usd=None, max_in_flight=2)
    try:
        for index in range(4):
            harness.client.submit_results.append(video_job(f"job-{index}", "pending"))
        harness.client.poll_results.extend(
            video_job("job-0", "in_progress") for _ in range(200)
        )

        outcomes = await asyncio.gather(
            *(harness.video_runner.submit(harness.video_request()) for _ in range(4))
        )

        submitted = [o for o in outcomes if o.state is GenerationState.SUBMITTED]
        rejected = [o for o in outcomes if o.state is GenerationState.REJECTED]

        assert len(submitted) == 2
        assert all(o.error_reason is ErrorReason.IN_FLIGHT_LIMIT for o in rejected)
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_a_failed_job_reservation_nets_to_zero(harness):
    harness.client.image_results.append(
        ProviderError("upstream exploded", status_code=500)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.FAILED
    rows = harness.db.list_ledger_for_generation(outcome.generation_id)
    assert [row.kind for row in rows].count(LedgerKind.REVERSAL) == 1
    assert harness.ledger_total(outcome.generation_id) == Decimal("0")


async def test_a_completed_job_with_unknown_cost_marks_the_day_a_lower_bound(harness):
    # Spec section 3.4: the cap over-counts rather than under-counts, and zero
    # is never recorded as the charge.
    harness.client.image_results.append(harness.image_result(cost=None))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    spend = harness.ledger.spend_for_day()
    assert spend.is_lower_bound is True
    assert spend.total > Decimal("0")


async def test_the_gated_row_is_excluded_exactly_once(harness):
    """Pins the arithmetic: the gate must not subtract its own row a second
    time on top of the SQL exclusion, or the ceiling would be off by one."""
    harness.client.submit_results.append(video_job("job-0", "pending"))
    harness.client.poll_results.extend(
        video_job("job-0", "in_progress") for _ in range(200)
    )
    try:
        outcome = await harness.video_runner.submit(harness.video_request())
        assert outcome.state is GenerationState.SUBMITTED

        # One non-terminal row exists. Excluding it yields zero, never minus one.
        assert harness.db.count_in_flight() == 1
        assert (
            harness.db.count_in_flight(exclude_generation_id=outcome.generation_id) == 0
        )
        assert harness.db.count_in_flight(exclude_generation_id="no-such-id") == 1
    finally:
        await harness.video_runner.shutdown()


def test_jobs_package_never_imports_web():
    # Dependency direction is one-way (spec section 4.1): web imports jobs.
    package_dir = Path(jobs_package.__file__).parent
    offenders = [
        source.name
        for source in package_dir.glob("*.py")
        if "higgshole.web" in source.read_text(encoding="utf-8")
    ]

    assert offenders == []
