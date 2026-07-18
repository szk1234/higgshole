import asyncio

import pytest

from higgshole.orclient.errors import IndeterminateError, RateLimitError
from higgshole.store.db import ErrorReason, GenerationState, InputRole
from tests.jobs.fakes import video_job


@pytest.fixture
def submitted(harness):
    """A video runner scripted to accept one submission and poll forever."""
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        video_job("job-1", "in_progress") for _ in range(50)
    )
    return harness


async def test_submit_returns_as_soon_as_the_job_id_is_committed(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())

    assert outcome.state is GenerationState.SUBMITTED
    assert outcome.file_path is None
    await submitted.video_runner.shutdown()


async def test_the_provider_job_id_is_persisted_before_polling_starts(submitted):
    # Spec section 4.3 durability rule: if the process dies here, resume.py
    # can still find the job.
    outcome = await submitted.video_runner.submit(submitted.video_request())

    stored = submitted.db.get_generation(outcome.generation_id)
    assert stored.provider_job_id == "job-1"
    assert stored.state is GenerationState.SUBMITTED
    await submitted.video_runner.shutdown()


async def test_submit_attaches_exactly_one_poller_per_generation(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())

    pollers = submitted.video_runner.active_pollers()
    assert list(pollers) == [outcome.generation_id]
    await submitted.video_runner.shutdown()


async def test_attach_poller_is_idempotent(submitted):
    # Boot reattachment must never double-download a paid generation.
    outcome = await submitted.video_runner.submit(submitted.video_request())

    first = submitted.video_runner.active_pollers()[outcome.generation_id]
    second = submitted.video_runner.attach_poller(
        outcome.generation_id, reservation=None
    )

    assert second is first
    await submitted.video_runner.shutdown()


async def test_validation_failure_is_rejected_before_submit(harness):
    outcome = await harness.video_runner.submit(
        harness.video_request(params={"duration": 7, "resolution": "720p"})
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION
    assert harness.client.calls == []


async def test_indeterminate_submit_failure_is_never_retried(harness):
    harness.client.submit_results.append(IndeterminateError("reset after send"))

    outcome = await harness.video_runner.submit(harness.video_request())

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.INDETERMINATE
    assert harness.client.call_names().count("submit_video") == 1


async def test_a_rate_limit_on_submit_is_retried(harness):
    harness.client.submit_results.extend(
        [RateLimitError("slow down", status_code=429), video_job("job-9", "pending")]
    )
    harness.client.poll_results.extend(
        video_job("job-9", "in_progress") for _ in range(50)
    )

    outcome = await harness.video_runner.submit(harness.video_request())

    assert outcome.state is GenerationState.SUBMITTED
    assert harness.client.call_names().count("submit_video") == 2
    await harness.video_runner.shutdown()


async def test_frame_images_are_sent_with_their_frame_type(harness):
    asset_id = harness.upload("first.png")
    harness.client.submit_results.append(video_job("job-2", "pending"))
    harness.client.poll_results.extend(
        video_job("job-2", "in_progress") for _ in range(50)
    )

    await harness.video_runner.submit(
        harness.video_request(inputs=((asset_id, InputRole.FIRST_FRAME),))
    )

    sent = harness.client.last_call("submit_video")
    assert [frame_type for _, frame_type in sent["frame_images"]] == ["first_frame"]
    assert sent["input_references"] == []
    await harness.video_runner.shutdown()


async def test_shutdown_cancels_every_poller(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())
    task = submitted.video_runner.active_pollers()[outcome.generation_id]

    await submitted.video_runner.shutdown()

    assert task.done()
    assert submitted.video_runner.active_pollers() == {}
    # Rows left mid-flight are picked up by resume.py at the next boot.
    assert submitted.db.get_generation(outcome.generation_id).state in {
        GenerationState.SUBMITTED,
        GenerationState.RUNNING,
    }
    assert isinstance(asyncio.get_running_loop(), asyncio.AbstractEventLoop)
