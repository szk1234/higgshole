from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from higgshole.orclient.errors import (
    InsufficientCreditsError,
    ModerationError,
    ProviderError,
)
from higgshole.orclient.types import KeyStatus
from higgshole.web.api import map_openrouter_error, mask_key
from tests.web.fakes import FakeClient, build_test_state


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


@pytest.mark.parametrize(
    "value",
    ["sk-or-v1-abcdef0123456789", "sk-or-v1-wxyz", None, ""],
)
def test_mask_key_never_reveals_more_than_four_characters(value):
    masked = mask_key(value)

    if not value:
        assert masked is None
        return

    assert masked is not None
    assert masked.startswith("...")
    assert len(masked.removeprefix("...")) <= 4
    assert masked.removeprefix("...") == value[-4:]
    assert value not in masked


def test_error_response_builds_a_uniform_body():
    from higgshole.catalog.validation import Severity, ValidationIssue
    from higgshole.web.api import error_response

    exc = error_response(
        422,
        "validation_failed",
        "bad request",
        issues=[
            ValidationIssue(
                parameter="duration",
                value="7",
                severity=Severity.HARD,
                message="unsupported",
            )
        ],
    )

    assert exc.status_code == 422
    assert exc.detail["error"] == "validation_failed"
    assert exc.detail["issues"][0]["parameter"] == "duration"


def test_a_provider_credit_limit_is_named_distinctly_from_the_local_cap():
    # Spec section 10: the operator must know which guard tripped.
    exc = map_openrouter_error(
        InsufficientCreditsError("out of credit", status_code=402)
    )

    assert exc.status_code == 402
    assert exc.detail["error"] == "provider_credit_limit"


def test_a_moderation_refusal_has_its_own_code():
    exc = map_openrouter_error(ModerationError("content policy", status_code=400))

    assert exc.detail["error"] == "moderation_refused"


def test_an_upstream_failure_is_reported_as_provider_unavailable():
    exc = map_openrouter_error(ProviderError("upstream", status_code=502))

    assert exc.status_code == 502
    assert exc.detail["error"] == "provider_unavailable"


def test_models_are_returned_with_their_discovered_capabilities(api):
    client, _ = api

    payload = client.get("/api/models").json()
    by_id = {entry["id"]: entry for entry in payload}

    assert by_id["kwaivgi/kling-v3.0-pro"]["supported_durations"] == [5, 10]
    assert by_id["openai/sora-2-pro"]["supported_frame_images"] == []
    assert by_id["openai/gpt-image-2"]["max_input_references"] == 16


def test_models_can_be_filtered_by_kind(api):
    client, _ = api

    payload = client.get("/api/models", params={"kind": "video"}).json()

    assert {entry["kind"] for entry in payload} == {"video"}


def test_favourite_models_are_flagged(api):
    client, state = api
    state.db.set_setting("favourite_models", '["openai/sora-2-pro"]')

    payload = client.get("/api/models").json()
    favourites = {entry["id"] for entry in payload if entry["is_favourite"]}

    assert favourites == {"openai/sora-2-pro"}


def test_projects_can_be_listed_and_created(api):
    client, _ = api

    created = client.post("/api/projects", json={"name": "Coast Shoot"})
    assert created.status_code == 201
    assert created.json()["slug"] == "coast-shoot"

    slugs = {entry["slug"] for entry in client.get("/api/projects").json()}
    assert {"unsorted", "coast-shoot"} <= slugs


def test_a_duplicate_project_is_rejected_with_409(api):
    client, _ = api
    client.post("/api/projects", json={"name": "Coast Shoot"})

    conflict = client.post("/api/projects", json={"name": "Coast Shoot"})

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "validation_failed"


def test_budget_reports_the_provider_figures_as_strings(api):
    # Spec section 3.2: provider figures are authoritative, and money crosses
    # the boundary as a string so no float rounding can occur.
    client, _ = api

    payload = client.get("/api/budget").json()

    assert payload["provider_available"] is True
    assert payload["provider_remaining_usd"] == "74.5"
    assert payload["cap_usd"] is None
    assert payload["spent_today_usd"] == "0"
    assert payload["max_in_flight"] == 3


def test_budget_marks_the_figures_local_only_when_the_key_call_fails(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(
        db=db,
        paths=media_paths,
        client=FakeClient(error=ProviderError("down", status_code=503)),
    )
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app) as client:
        payload = client.get("/api/budget").json()

    assert payload["provider_available"] is False
    assert payload["provider_remaining_usd"] is None


def test_the_key_status_is_cached_between_requests(api, monkeypatch):
    client, state = api
    calls = {"n": 0}
    original = state.client_factory("image").get_key_status

    async def counting() -> KeyStatus:
        calls["n"] += 1
        return await original()

    monkeypatch.setattr(state.client_factory("image"), "get_key_status", counting)

    client.get("/api/budget")
    client.get("/api/budget")

    assert calls["n"] == 1
    assert Decimal(client.get("/api/budget").json()["provider_limit_usd"]) == Decimal(
        "100"
    )
