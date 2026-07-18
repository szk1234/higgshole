from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from higgshole.catalog.validation import Severity, ValidationIssue
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState
from tests.web.fakes import build_test_state, failed_outcome


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_an_exact_estimate_is_returned_as_a_string(api):
    client, _ = api

    payload = client.post(
        "/api/estimate",
        params={"kind": "image"},
        json={"model": "openai/gpt-image-2", "prompt": "a cat"},
    ).json()

    assert payload["estimate_unavailable"] is None
    assert isinstance(payload["amount_usd"], str)


def test_an_unavailable_estimate_returns_null_and_a_reason(api, monkeypatch):
    # Spec section 3.2 Layer 3: never a fabricated number.
    from higgshole.budget.estimator import Estimate, EstimateUnavailable
    from higgshole.web import api as api_module

    monkeypatch.setattr(
        api_module,
        "estimate_image_cost",
        lambda *a, **k: Estimate(
            amount=None,
            reason=EstimateUnavailable.TOKEN_PRICED,
            detail="priced per token with no published conversion",
        ),
    )
    client, _ = api

    payload = client.post(
        "/api/estimate",
        params={"kind": "image"},
        json={"model": "openai/gpt-image-2", "prompt": "a cat"},
    ).json()

    assert payload["amount_usd"] is None
    assert payload["estimate_unavailable"] == "token_priced"


def test_image_generation_returns_the_finished_generation(api):
    client, state = api

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "neon city", "quality": "high"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "COMPLETE"
    assert body["project_slug"] == "unsorted"
    assert state.image_runner.requests[0].params["quality"] == "high"


def test_a_hard_validation_failure_is_422_with_the_issues(api):
    client, state = api
    state.image_runner.issues = [
        ValidationIssue(
            parameter="quality",
            value="ultra",
            severity=Severity.HARD,
            message="openai/gpt-image-2 does not support quality=ultra.",
        )
    ]

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "x", "quality": "ultra"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_failed"
    assert body["issues"][0]["parameter"] == "quality"
    assert state.image_runner.requests == []


def test_an_advisory_issue_does_not_block_dispatch(api):
    # Spec section 2.7: a value the catalogue omits but pricing covers is
    # warned about and sent.
    client, state = api
    state.image_runner.issues = [
        ValidationIssue(
            parameter="resolution",
            value="1080p",
            severity=Severity.ADVISORY,
            message="not declared but priced",
        )
    ]

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 200
    assert len(state.image_runner.requests) == 1


def test_a_cap_rejection_is_402_local_daily_cap(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.image_runner.outcome = failed_outcome(
        state.db,
        project.id,
        reason=ErrorReason.CAP_EXCEEDED,
        state=GenerationState.REJECTED,
    )

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 402
    assert response.json()["error"] == "local_daily_cap"


def test_an_in_flight_rejection_is_429(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.image_runner.outcome = failed_outcome(
        state.db,
        project.id,
        reason=ErrorReason.IN_FLIGHT_LIMIT,
        state=GenerationState.REJECTED,
    )

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 429
    assert response.json()["error"] == "in_flight_limit"


def test_video_submission_returns_202_without_blocking(api):
    # Spec section 6.2: a multi-minute render inside one call invites timeouts.
    client, state = api

    response = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach", "duration": 5},
    )

    assert response.status_code == 202
    assert response.json()["state"] == "SUBMITTED"


def test_an_unknown_model_is_404_model_not_found(api):
    client, _ = api

    response = client.post(
        "/api/generate/image", json={"model": "nope/nothing", "prompt": "x"}
    )

    assert response.status_code == 404
    assert response.json()["error"] == "model_not_found"


def test_an_unknown_project_is_404_project_not_found(api):
    client, _ = api

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "x", "project": "ghost"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "project_not_found"


def test_get_job_returns_the_current_state(api):
    client, state = api
    created = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach"},
    ).json()

    fetched = client.get(f"/api/jobs/{created['id']}").json()

    assert fetched["id"] == created["id"]
    assert fetched["state"] == "SUBMITTED"


def test_get_job_long_polls_until_the_state_is_terminal(api, monkeypatch):
    client, state = api
    created = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach"},
    ).json()

    from higgshole.web import api as api_module

    monkeypatch.setattr(api_module, "JOB_POLL_INTERVAL_S", 0.01)

    calls = {"n": 0}
    real = state.db.get_generation

    def eventually_complete(gen_id: str):
        calls["n"] += 1
        row = real(gen_id)
        if row is not None and calls["n"] >= 3:
            return replace(row, state=GenerationState.COMPLETE)
        return row

    monkeypatch.setattr(state.db, "get_generation", eventually_complete)

    fetched = client.get(f"/api/jobs/{created['id']}", params={"wait_seconds": 5}).json()

    assert fetched["state"] == "COMPLETE"
    assert calls["n"] >= 3


def test_an_unknown_job_is_404_generation_not_found(api):
    client, _ = api

    response = client.get("/api/jobs/000000000000")

    assert response.status_code == 404
    assert response.json()["error"] == "generation_not_found"


def test_listing_jobs_returns_only_in_flight_generations(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="finished",
        params={},
        state=GenerationState.COMPLETE,
    )
    running = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="in flight",
        params={},
        state=GenerationState.RUNNING,
    )

    ids = {entry["id"] for entry in client.get("/api/jobs").json()}

    assert running.id in ids
    assert all(entry["state"] != "COMPLETE" for entry in client.get("/api/jobs").json())
