import json

import httpx
import pytest
import respx

from higgshole.mcp_server import (
    TOOL_HANDLERS,
    TOOL_NAMES,
    HiggsHoleAPI,
    ToolError,
    tool_create_project,
    tool_delete_media,
    tool_get_budget,
    tool_get_media,
    tool_list_media,
    tool_list_projects,
    tool_upload_asset,
    with_local_path,
)

API = "http://127.0.0.1:8077"

UPLOAD = {
    "id": "0c118b4e77aa",
    "kind": "upload",
    "mime_type": "image/png",
    "bytes": 68,
    "width": 8,
    "height": 8,
    "duration_s": None,
    "local_path": "/srv/higgshole/media/projects/art/uploads/ref.png",
    "url": "/media/projects/art/uploads/ref.png",
    "created_at": "2026-07-18T14:29:00+00:00",
}


@pytest.fixture
async def api():
    client = HiggsHoleAPI(API)
    try:
        yield client
    finally:
        await client.aclose()


def test_every_declared_tool_has_a_handler():
    assert set(TOOL_HANDLERS) == set(TOOL_NAMES)


def test_with_local_path_absolutises_a_relative_url():
    result = with_local_path(dict(UPLOAD), API)

    assert result["url"] == f"{API}/media/projects/art/uploads/ref.png"
    assert result["local_path"] == UPLOAD["local_path"]


def test_with_local_path_leaves_an_absolute_url_alone():
    asset = {**UPLOAD, "url": "http://example.invalid/media/x.png"}

    assert with_local_path(asset, API)["url"] == "http://example.invalid/media/x.png"


def test_with_local_path_rejects_an_asset_missing_its_local_path():
    asset = {k: v for k, v in UPLOAD.items() if k != "local_path"}

    with pytest.raises(ToolError) as caught:
        with_local_path(asset, API)

    assert caught.value.code == "internal_error"


@respx.mock
async def test_upload_asset_posts_the_file_as_multipart(api, tmp_path):
    source = tmp_path / "reference.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    route = respx.post(f"{API}/api/uploads").mock(return_value=httpx.Response(200, json=UPLOAD))

    result = await tool_upload_asset(api, path=str(source), project="art")

    body = route.calls.last.request.read()
    assert b"reference.png" in body
    assert b"\x89PNG" in body
    assert b"art" in body
    assert result["id"] == "0c118b4e77aa"


@respx.mock
async def test_upload_asset_returns_both_forms_of_location(api, tmp_path):
    source = tmp_path / "reference.png"
    source.write_bytes(b"x")
    respx.post(f"{API}/api/uploads").mock(return_value=httpx.Response(200, json=UPLOAD))

    result = await tool_upload_asset(api, path=str(source))

    assert result["local_path"].startswith("/")
    assert result["url"] == f"{API}/media/projects/art/uploads/ref.png"


@respx.mock
async def test_upload_asset_rejects_a_missing_file_without_any_http_call(api, tmp_path):
    route = respx.post(f"{API}/api/uploads").mock(return_value=httpx.Response(200, json=UPLOAD))

    with pytest.raises(ToolError) as caught:
        await tool_upload_asset(api, path=str(tmp_path / "absent.png"))

    assert caught.value.code == "asset_not_found"
    assert route.call_count == 0


@respx.mock
async def test_list_media_forwards_every_filter(api):
    route = respx.get(f"{API}/api/media").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "limit": 10, "offset": 0})
    )

    await tool_list_media(
        api,
        project="art",
        kind="video",
        model="google/veo-3.1",
        created_after="2026-07-01T00:00:00+00:00",
        created_before="2026-08-01T00:00:00+00:00",
        limit=10,
        offset=20,
    )

    params = route.calls.last.request.url.params
    assert params["project"] == "art"
    assert params["kind"] == "video"
    assert params["model"] == "google/veo-3.1"
    assert params["created_after"].startswith("2026-07-01")
    assert params["created_before"].startswith("2026-08-01")
    assert params["limit"] == "10"
    assert params["offset"] == "20"


@respx.mock
async def test_list_media_omits_unset_filters(api):
    route = respx.get(f"{API}/api/media").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
    )

    await tool_list_media(api)

    params = route.calls.last.request.url.params
    assert set(params) == {"limit", "offset"}


@respx.mock
async def test_list_media_absolutises_every_item_url(api):
    respx.get(f"{API}/api/media").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "a3f21c9d4e07", "state": "COMPLETE", "asset": dict(UPLOAD)}],
                "total": 1,
                "limit": 50,
                "offset": 0,
            },
        )
    )

    result = await tool_list_media(api)

    assert result["items"][0]["asset"]["url"].startswith(API)


@respx.mock
async def test_get_media_returns_lineage_and_a_string_cost(api):
    respx.get(f"{API}/api/media/a3f21c9d4e07").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "a3f21c9d4e07",
                "state": "COMPLETE",
                "cost_usd": "0.04",
                "cost_known": True,
                "asset": dict(UPLOAD),
                "inputs": [{"asset_id": "0c118b4e77aa", "role": "input_reference", "position": 0}],
            },
        )
    )

    result = await tool_get_media(api, generation_id="a3f21c9d4e07")

    assert result["inputs"][0]["role"] == "input_reference"
    assert isinstance(result["cost_usd"], str)


@respx.mock
async def test_an_unknown_cost_stays_null_through_get_media(api):
    # Spec section 3.4: never 0 for unknown.
    respx.get(f"{API}/api/media/a3f21c9d4e07").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "a3f21c9d4e07",
                "state": "COMPLETE",
                "cost_usd": None,
                "cost_known": False,
                "asset": dict(UPLOAD),
            },
        )
    )

    result = await tool_get_media(api, generation_id="a3f21c9d4e07")

    assert result["cost_usd"] is None
    assert result["cost_known"] is False


@respx.mock
async def test_delete_media_reports_the_deletion(api):
    route = respx.delete(f"{API}/api/media/a3f21c9d4e07").mock(return_value=httpx.Response(204))

    result = await tool_delete_media(api, generation_id="a3f21c9d4e07")

    assert result == {"deleted": True, "generation_id": "a3f21c9d4e07"}
    assert route.call_count == 1


@respx.mock
async def test_list_projects_returns_the_project_array(api):
    respx.get(f"{API}/api/projects").mock(
        return_value=httpx.Response(
            200, json=[{"id": "p1", "slug": "unsorted", "name": "Unsorted", "item_count": 0}]
        )
    )

    projects = await tool_list_projects(api)

    assert projects[0]["slug"] == "unsorted"


@respx.mock
async def test_create_project_posts_the_name(api):
    route = respx.post(f"{API}/api/projects").mock(
        return_value=httpx.Response(201, json={"id": "p2", "slug": "concept-art"})
    )

    result = await tool_create_project(api, name="Concept Art")

    assert json.loads(route.calls.last.request.read()) == {"name": "Concept Art"}
    assert result["slug"] == "concept-art"


@respx.mock
async def test_get_budget_returns_provider_and_local_figures(api):
    respx.get(f"{API}/api/budget").mock(
        return_value=httpx.Response(
            200,
            json={
                "provider_remaining_usd": "74.50",
                "provider_available": True,
                "cap_usd": "5.00",
                "spent_today_usd": "1.20",
                "remaining_today_usd": "3.80",
                "is_lower_bound": False,
                "in_flight": 1,
                "max_in_flight": 3,
            },
        )
    )

    budget = await tool_get_budget(api)

    assert budget["provider_remaining_usd"] == "74.50"
    assert budget["in_flight"] == 1
