import json
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def _make_generation(state, *, kind=GenerationKind.IMAGE, prompt="neon city"):
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=kind,
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
    asset = state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )
    return row, asset


def test_uploading_a_file_creates_an_upload_asset(api):
    client, state = api

    response = client.post(
        "/api/uploads",
        files={"file": ("reference.png", PNG_1X1, "image/png")},
        data={"project": "unsorted"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "upload"
    assert body["mime_type"] == "image/png"
    assert body["url"].startswith("/media/projects/unsorted/uploads/")
    assert (state.paths.root / body["url"].removeprefix("/media/")).is_file()


def test_an_unsupported_upload_type_is_415(api):
    client, _ = api

    response = client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


def test_an_oversized_upload_is_413(api, monkeypatch):
    from higgshole.web import api as api_module

    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 8)
    client, _ = api

    response = client.post(
        "/api/uploads", files={"file": ("reference.png", PNG_1X1, "image/png")}
    )

    assert response.status_code == 413
    assert response.json()["error"] == "upload_too_large"


def test_media_listing_paginates_and_reports_the_total(api):
    client, state = api
    for index in range(3):
        _make_generation(state, prompt=f"item {index}")

    payload = client.get("/api/media", params={"limit": 2, "offset": 0}).json()

    assert payload["total"] == 3
    assert len(payload["items"]) == 2
    assert payload["limit"] == 2


def test_media_listing_filters_by_kind(api):
    client, state = api
    _make_generation(state, kind=GenerationKind.IMAGE)
    _make_generation(state, kind=GenerationKind.VIDEO)

    payload = client.get("/api/media", params={"kind": "video"}).json()

    assert {item["kind"] for item in payload["items"]} == {"video"}


def test_media_detail_includes_the_asset_and_its_urls(api):
    client, state = api
    row, _ = _make_generation(state)

    body = client.get(f"/api/media/{row.id}").json()

    assert body["asset"]["url"].startswith("/media/")
    assert body["asset"]["local_path"].endswith(f"{row.id}.png")
    assert body["thumb_url"] == f"/thumbs/unsorted/{row.id}.webp"


def test_media_detail_includes_lineage(api):
    client, state = api
    parent, parent_asset = _make_generation(state, prompt="parent")
    child, _ = _make_generation(state, prompt="child")
    state.db.add_generation_input(
        generation_id=child.id,
        asset_id=parent_asset.id,
        role="input_reference",
        position=0,
    )

    body = client.get(f"/api/media/{child.id}").json()

    assert body["inputs"][0]["asset_id"] == parent_asset.id
    assert body["inputs"][0]["generation_id"] == parent.id


def test_deleting_media_removes_the_row_and_the_file(api):
    client, state = api
    row, _ = _make_generation(state)
    on_disk = state.paths.root / f"projects/unsorted/images/{row.id}.png"

    response = client.delete(f"/api/media/{row.id}")

    assert response.status_code == 204
    assert state.db.get_generation(row.id) is None
    assert not on_disk.exists()


def test_deleting_unknown_media_is_404(api):
    client, _ = api

    response = client.delete("/api/media/000000000000")

    assert response.status_code == 404
    assert response.json()["error"] == "generation_not_found"


def test_settings_never_return_a_full_api_key(api):
    # Spec section 7: keys are write-only through the UI.
    client, state = api
    secret = "sk-or-v1-abcdef0123456789"
    state.db.set_setting("openrouter_api_key", secret)

    body = client.get("/api/settings").json()

    assert body["openrouter_api_key_masked"] == "...6789"
    assert secret not in json.dumps(body)


def test_saving_a_key_stores_it_and_returns_only_a_mask(api):
    client, state = api
    secret = "sk-or-v1-zyxwvu9876543210"

    body = client.put("/api/settings", json={"openrouter_api_key": secret}).json()

    assert body["openrouter_api_key_masked"] == "...3210"
    assert secret not in json.dumps(body)
    assert state.db.get_setting("openrouter_api_key") == secret


def test_a_malformed_key_is_rejected_before_it_is_stored(api):
    # A key from another provider otherwise yields the provider's misleading
    # "Missing Authentication header" (spec section 7).
    client, state = api

    response = client.put(
        "/api/settings", json={"openrouter_api_key": "sk-proj-abcdef0123456789"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "validation_failed"
    assert state.db.get_setting("openrouter_api_key") is None


def test_a_key_saved_through_the_api_is_used_by_the_next_generation(api):
    # Spec section 8: keys may alternatively be set through the UI, so the
    # client factory resolves through the database on every call instead of
    # reading the environment once at startup.
    from higgshole.web.app import resolve_api_key

    client, state = api
    secret = "sk-or-v1-abcdef0123456789"

    client.put("/api/settings", json={"openrouter_api_key_image": secret})

    assert resolve_api_key(state.db, state.settings, "image") == secret


def test_a_cap_saved_through_the_api_is_enforced(api):
    # The runners hold the gate, so a saved cap has to reach that live
    # instance; otherwise the UI accepts a cap that guards nothing.
    client, state = api

    client.put("/api/settings", json={"daily_cap_usd": "1.50"})

    assert state.gate.cap == Decimal("1.50")
    assert state.gate.cap_is_set is True

    client.put("/api/settings", json={"daily_cap_usd": ""})

    assert state.gate.cap is None


def test_settings_report_catalogue_freshness_and_transport(api):
    client, _ = api

    body = client.get("/api/settings").json()

    assert body["catalog"]["is_stale"] is False
    assert body["reference_transport"] == "data_uri"
    assert isinstance(body["video_references_supported"], bool)


def test_a_manual_catalogue_refresh_reports_its_status(api):
    client, state = api

    body = client.post("/api/settings/catalog/refresh").json()

    assert body["is_stale"] is False
    assert state.catalog.refresh_calls >= 1


def test_rescan_rebuilds_rows_from_sidecars(api):
    client, state = api
    relative = "projects/unsorted/images/20260718-143022_a3f21c9d4e07_neon.png"
    media = state.paths.root / relative
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(PNG_1X1)
    sidecar = media.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "sidecar_version": 1,
                "id": "a3f21c9d4e07",
                "kind": "image",
                "project_slug": "unsorted",
                "model": "openai/gpt-image-2",
                "prompt": "neon city street at night",
                "params": {"quality": "high"},
                "inputs": [],
                "provider": {"job_id": None, "generation_id": "gen-1"},
                "media": {
                    "relative_path": relative,
                    "mime_type": "image/png",
                    "bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "duration_s": None,
                },
                "cost": {"amount_usd": "0.04", "known": True},
                "created_at": "2026-07-18T14:30:22.104883+00:00",
                "completed_at": "2026-07-18T14:30:29.551204+00:00",
            }
        ),
        encoding="utf-8",
    )

    body = client.post("/api/settings/rescan").json()

    assert body["sidecars_read"] == 1
    assert body["generations_created"] == 1
    assert body["errors"] == []
    assert state.db.get_generation("a3f21c9d4e07") is not None
