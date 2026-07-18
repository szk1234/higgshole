import pytest
from starlette.testclient import TestClient

from higgshole.store.db import GenerationKind, GenerationState
from tests.web.fakes import build_test_state


@pytest.fixture
def pages(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_controls_offer_only_the_durations_the_model_declares(pages):
    # Spec section 6.1: controls are rendered from discovered capabilities;
    # an option the model does not support is never offered.
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro"},
    ).text

    assert 'value="5"' in html
    assert 'value="10"' in html
    assert 'value="4"' not in html
    assert 'value="8"' not in html


def test_controls_offer_only_the_resolutions_the_model_declares(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "openai/sora-2-pro"},
    ).text

    assert 'value="720p"' in html
    assert 'value="1080p"' in html
    assert 'value="480p"' not in html


def test_a_text_only_model_is_offered_no_frame_slots(pages):
    # openai/sora-2-pro accepts no frame images at all (spec section 2.7).
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "openai/sora-2-pro"},
    ).text

    assert "first_frame_asset_id" not in html
    assert "last_frame_asset_id" not in html


def test_a_first_and_last_frame_model_is_offered_both_slots(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro"},
    ).text

    assert "first_frame_asset_id" in html
    assert "last_frame_asset_id" in html


def test_reference_slots_appear_only_in_the_quantity_the_model_accepts(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "image", "model": "recraft/recraft-v4.1"},
    ).text

    assert html.count('name="input_reference_asset_ids"') == 1


def test_an_estimate_partial_shows_a_reason_rather_than_a_number(pages, monkeypatch):
    from higgshole.budget.estimator import Estimate, EstimateUnavailable
    from higgshole.web import api as api_module

    monkeypatch.setattr(
        api_module,
        "estimate_video_cost",
        lambda *a, **k: Estimate(
            amount=None,
            reason=EstimateUnavailable.VIDEO_TOKEN_PRICED,
            detail="priced in video tokens with no published conversion table",
        ),
    )
    client, _ = pages

    html = client.get(
        "/partials/estimate",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro", "prompt": "x"},
    ).text

    assert "no published conversion table" in html
    assert "$" not in html


def test_an_estimate_partial_shows_the_amount_when_it_is_exact(pages):
    client, _ = pages

    html = client.get(
        "/partials/estimate",
        params={"kind": "image", "model": "openai/gpt-image-2", "prompt": "x"},
    ).text

    assert "USD" in html


def test_the_library_grid_partial_is_a_fragment_not_a_document(pages):
    client, state = pages
    project = state.db.get_project_by_slug("unsorted")
    state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="fragment probe",
        params={},
        state=GenerationState.COMPLETE,
    )

    html = client.get("/partials/library-grid").text

    assert "<html" not in html.lower()
    assert "fragment probe" in html


def test_the_job_row_partial_renders_the_current_state(pages):
    client, state = pages
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="row probe",
        params={},
        state=GenerationState.RUNNING,
    )

    html = client.get("/partials/job-row", params={"gen_id": row.id}).text

    assert "RUNNING" in html
    assert row.id in html
    assert "<html" not in html.lower()
