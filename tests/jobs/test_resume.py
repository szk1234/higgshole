from datetime import UTC, datetime, timedelta
from decimal import Decimal

from higgshole.jobs.resume import ResumeReport, reservation_for, resume_pending_jobs
from higgshole.store.db import (
    ErrorReason,
    GenerationKind,
    GenerationState,
    LedgerKind,
)
from tests.jobs.fakes import MP4_BYTES, video_job


def _backdate(harness, gen_id: str, *, minutes: int) -> None:
    """Rewrite created_at so the row looks older than the ceiling.

    Written straight to SQLite because no application code may rewrite a
    creation timestamp; only a test needs this.
    """
    stale = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    with harness.db.transaction() as connection:
        connection.execute(
            "UPDATE generations SET created_at = ? WHERE id = ?", (stale, gen_id)
        )


async def _submitted_row(harness, *, job_id: str = "job-1") -> str:
    """A video row parked in SUBMITTED with its poller detached, as a crash
    would leave it."""
    harness.client.submit_results.append(video_job(job_id, "pending"))
    harness.client.poll_results.extend(
        video_job(job_id, "in_progress") for _ in range(50)
    )
    outcome = await harness.video_runner.submit(harness.video_request())
    await harness.video_runner.shutdown()
    return outcome.generation_id


async def test_a_submitted_video_row_is_reattached(harness):
    gen_id = await _submitted_row(harness)
    harness.client.poll_results.extend(
        video_job("job-1", "in_progress") for _ in range(50)
    )

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.reattached == (gen_id,)
    assert gen_id in harness.video_runner.active_pollers()
    await harness.video_runner.shutdown()


async def test_an_image_row_is_never_reattached(harness):
    # Image rows can never occupy SUBMITTED or RUNNING, but resume filters on
    # kind anyway as a defence against a corrupted row (spec section 4.3).
    row = harness.db.create_generation(
        project_id=harness.project.id,
        kind=GenerationKind.IMAGE,
        model="test/image-model",
        prompt="a cat",
        params={},
        state=GenerationState.PENDING,
    )
    with harness.db.transaction() as connection:
        connection.execute(
            "UPDATE generations SET state = 'RUNNING', provider_job_id = 'bogus' "
            "WHERE id = ?",
            (row.id,),
        )

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report == ResumeReport(reattached=(), timed_out=(), orphaned=())
    assert harness.video_runner.active_pollers() == {}
    assert harness.client.call_names().count("get_video_job") == 0


async def test_a_running_row_older_than_the_ceiling_is_failed_with_timeout(harness):
    gen_id = await _submitted_row(harness)
    _backdate(harness, gen_id, minutes=harness.settings.job_timeout_minutes + 5)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.timed_out == (gen_id,)
    stored = harness.db.get_generation(gen_id)
    assert stored.state is GenerationState.FAILED
    assert stored.error_reason is ErrorReason.TIMEOUT
    assert harness.video_runner.active_pollers() == {}


async def test_a_resumable_row_without_a_job_id_is_failed_as_indeterminate(harness):
    # The process died between the gate and the submit response, so the
    # submission may already have been billed.
    row = harness.db.create_generation(
        project_id=harness.project.id,
        kind=GenerationKind.VIDEO,
        model="test/video-model",
        prompt="a beach",
        params={},
        state=GenerationState.PENDING,
    )
    harness.db.set_generation_state(row.id, GenerationState.SUBMITTED)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.orphaned == (row.id,)
    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.FAILED
    assert stored.error_reason is ErrorReason.INDETERMINATE


async def test_reservations_are_rebuilt_from_the_ledger(harness):
    gen_id = await _submitted_row(harness)

    rebuilt = reservation_for(harness.ledger, gen_id)

    assert rebuilt is not None
    assert rebuilt.generation_id == gen_id
    assert rebuilt.amount > Decimal("0")


async def test_a_rebuilt_reservation_is_never_marked_exact(harness):
    """Ledger.reserve always writes cost_known=False, so exactness is not
    persisted and cannot be recovered at boot. Pinned as False rather than
    read back from the row, which would conflate two different booleans.
    Affects reporting only — the amount is still recovered exactly."""
    gen_id = await _submitted_row(harness)

    rebuilt = reservation_for(harness.ledger, gen_id)

    assert rebuilt.from_exact_estimate is False
    rows = harness.db.list_ledger_for_generation(gen_id)
    assert all(
        row.cost_known is False for row in rows if row.kind is LedgerKind.RESERVATION
    )


async def test_reservation_for_returns_none_once_settled(harness):
    gen_id = await _submitted_row(harness)
    reservation = reservation_for(harness.ledger, gen_id)
    await harness.gate.release(reservation, actual_cost=None, succeeded=False)

    assert reservation_for(harness.ledger, gen_id) is None


async def test_a_completed_row_is_not_reattached(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.20"))
    harness.client.download_results.append(MP4_BYTES)
    outcome = await harness.video_runner.submit(harness.video_request())
    await harness.video_runner.active_pollers()[outcome.generation_id]

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.reattached == ()


async def test_resume_survives_a_simulated_restart_mid_flight(harness):
    """The headline guarantee: a job in flight when the process dies is picked
    up at the next boot and completes normally, with its reservation intact."""
    gen_id = await _submitted_row(harness, job_id="job-7")

    # Everything in memory is gone; only SQLite and the disk survive.
    assert harness.video_runner.active_pollers() == {}
    assert harness.db.get_generation(gen_id).state is GenerationState.SUBMITTED

    harness.client.poll_results.clear()
    harness.client.poll_results.append(video_job("job-7", "completed", cost="0.75"))
    harness.client.download_results.append(MP4_BYTES)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )
    outcome = await harness.video_runner.active_pollers()[gen_id]

    assert report.reattached == (gen_id,)
    assert outcome.state is GenerationState.COMPLETE
    assert (harness.paths.root / outcome.file_path).read_bytes() == MP4_BYTES
    # The reservation was rebuilt from the ledger, not from memory, so the
    # actual cost reconciles exactly once.
    assert harness.ledger_total(gen_id) == Decimal("0.75")
    kinds = [row.kind for row in harness.db.list_ledger_for_generation(gen_id)]
    assert kinds.count(LedgerKind.RESERVATION) == 1
    assert kinds.count(LedgerKind.ACTUAL) == 1
