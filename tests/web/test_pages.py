import re

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from higgshole.web.pages import TEMPLATES_DIR
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def pages(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def _completed_generation(state, prompt="neon city street"):
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt=prompt,
        params={"quality": "high"},
        state=GenerationState.COMPLETE,
    )
    relative = f"projects/unsorted/images/{row.id}.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    state.db.set_generation_file(row.id, relative)
    state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )
    return row


def test_the_create_screen_renders_with_a_model_picker(pages):
    client, _ = pages

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'name="prompt"' in response.text
    assert "openai/gpt-image-2" in response.text


def test_the_library_screen_lists_completed_generations(pages):
    client, state = pages
    row = _completed_generation(state)

    response = client.get("/library")

    assert response.status_code == 200
    assert row.id in response.text
    assert "neon city street" in response.text


def test_the_detail_screen_shows_metadata_and_the_media_url(pages):
    client, state = pages
    row = _completed_generation(state)

    response = client.get(f"/library/{row.id}")

    assert response.status_code == 200
    assert f"/media/projects/unsorted/images/{row.id}.png" in response.text
    assert "openai/gpt-image-2" in response.text


def test_an_unknown_detail_id_is_404(pages):
    client, _ = pages

    assert client.get("/library/000000000000").status_code == 404


def test_the_jobs_screen_subscribes_to_the_event_stream(pages):
    client, _ = pages

    response = client.get("/jobs")

    assert response.status_code == 200
    assert "/events/jobs" in response.text


def test_the_settings_screen_shows_only_a_masked_key(pages):
    client, state = pages
    secret = "sk-or-v1-abcdef0123456789"
    state.db.set_setting("openrouter_api_key", secret)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "...6789" in response.text
    assert secret not in response.text


def test_no_template_references_an_external_host():
    # The UI must work on an offline LAN, so every asset is vendored.
    pattern = re.compile(r"""(?:src|href)\s*=\s*["'](https?:)?//""")

    offenders = [
        path.name
        for path in TEMPLATES_DIR.rglob("*.html")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_vendored_stylesheet_is_served(pages):
    client, _ = pages

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
