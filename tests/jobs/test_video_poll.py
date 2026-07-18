from decimal import Decimal

import pytest

from higgshole.orclient.errors import ProviderError
from higgshole.store.db import ErrorReason, GenerationState, LedgerKind
from tests.jobs.fakes import MP4_BYTES, Harness, video_job


async def _submit_and_wait(harness, *, request=None):
    outcome = await harness.video_runner.submit(request or harness.video_request())
    task = harness.video_runner.active_pollers().get(outcome.generation_id)
    if task is None:
        return outcome, outcome
    return outcome, await task


async def test_pending_then_completed_downloads_and_completes(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        [
            video_job("job-1", "in_progress"),
            video_job("job-1", "completed", cost="0.80", urls=("https://x/y.mp4",)),
        ]
    )
    harness.client.download_results.append(MP4_BYTES)

    submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost == Decimal("0.80")
    assert (harness.paths.root / final.file_path).read_bytes() == MP4_BYTES
    assert harness.events.states_for(submitted.generation_id) == [
        "PENDING",
        "SUBMITTED",
        "RUNNING",
        "DOWNLOADING",
        "COMPLETE",
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("failed", ErrorReason.PROVIDER_FAILED),
        ("cancelled", ErrorReason.PROVIDER_CANCELLED),
        ("expired", ErrorReason.PROVIDER_EXPIRED),
    ],
)
async def test_terminal_failure_statuses_map_to_their_reasons(harness, status, reason):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(
        video_job("job-1", status, error="upstream said no")
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.FAILED
    assert final.error_reason is reason
    assert harness.client.call_names().count("download_video") == 0


async def test_an_unrecognised_status_keeps_polling(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        [
            video_job("job-1", "reticulating"),
            video_job("job-1", "reticulating"),
            video_job("job-1", "completed", cost="0.10"),
        ]
    )
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert harness.client.call_names().count("get_video_job") == 3


async def test_the_wall_clock_ceiling_fails_the_job(tmp_path, stub_media):
    harness = Harness(tmp_path, job_timeout_minutes=1, poll_interval_seconds=5)
    try:
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(100)
        )

        _submitted, final = await _submit_and_wait(harness)

        assert final.state is GenerationState.FAILED
        assert final.error_reason is ErrorReason.TIMEOUT
        # 60s ceiling at a 5s cadence: bounded, and no test ever really slept.
        assert harness.clock.monotonic() >= 60
    finally:
        harness.db.close()


async def test_a_timeout_reverses_the_reservation(tmp_path, stub_media):
    harness = Harness(tmp_path, job_timeout_minutes=1, poll_interval_seconds=5)
    try:
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(100)
        )

        _submitted, final = await _submit_and_wait(harness)

        assert harness.ledger_total(final.generation_id) == Decimal("0")
    finally:
        harness.db.close()


async def test_a_502_download_is_retried_then_fails(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.50"))
    harness.client.download_results.extend(
        ProviderError("upstream", status_code=502) for _ in range(5)
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.FAILED
    assert final.error_reason is ErrorReason.DOWNLOAD_FAILED
    # max_retries=2 in the harness, so three attempts in total.
    assert harness.client.call_names().count("download_video") == 3


async def test_a_download_retry_that_succeeds_completes_the_job(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.50"))
    harness.client.download_results.extend(
        [ProviderError("upstream", status_code=502), MP4_BYTES]
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost == Decimal("0.50")


async def test_the_result_url_is_never_persisted(harness):
    # OpenRouter proxies from the upstream provider and publishes no retention
    # window, so a result URL must never become a durable reference
    # (spec section 2.5).
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(
        video_job("job-1", "completed", cost="0.50", urls=("https://storage/x.mp4",))
    )
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    stored = harness.db.get_generation(final.generation_id)
    assert "storage" not in (stored.file_path or "")
    assert "https://" not in str(stored.params)
    assets = harness.db.list_assets_for_generation(final.generation_id)
    assert all("https://" not in asset.file_path for asset in assets)


async def test_a_completed_job_with_null_cost_leaves_the_reservation_standing(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost=None))
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost is None

    rows = harness.db.list_ledger_for_generation(final.generation_id)
    assert LedgerKind.REVERSAL not in [row.kind for row in rows]
    assert harness.ledger_total(final.generation_id) > Decimal("0")


async def test_a_failed_job_nets_the_ledger_to_zero(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "failed", error="nope"))

    _submitted, final = await _submit_and_wait(harness)

    assert harness.ledger_total(final.generation_id) == Decimal("0")
    kinds = [
        row.kind for row in harness.db.list_ledger_for_generation(final.generation_id)
    ]
    assert LedgerKind.RESERVATION in kinds
    assert LedgerKind.REVERSAL in kinds
