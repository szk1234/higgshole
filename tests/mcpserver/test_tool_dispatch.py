import json

import httpx
import pytest
import respx

from higgshole.mcp_server import (
    HiggsHoleAPI,
    ToolError,
    dispatch,
    tool_generate_image,
    tool_generate_video,
    tool_get_job,
    tool_list_models,
)

API = "http://127.0.0.1:8077"

IMAGE_GENERATION = {
    "id": "a3f21c9d4e07",
    "kind": "image",
    "project_slug": "unsorted",
    "model": "openai/gpt-image-2",
    "prompt": "neon city",
    "state": "COMPLETE",
    "cost_usd": "0.04",
    "cost_known": True,
    "asset": {
        "id": "0c118b4e77aa",
        "kind": "output",
        "mime_type": "image/png",
        "bytes": 1843200,
        "width": 1920,
        "height": 1080,
        "duration_s": None,
        "local_path": "/srv/higgshole/media/projects/unsorted/images/a.png",
        "url": "/media/projects/unsorted/images/a.png",
        "created_at": "2026-07-18T14:30:29.551204+00:00",
    },
}


@pytest.fixture
async def api():
    client = HiggsHoleAPI(API)
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_list_models_forwards_the_kind_filter(api):
    route = respx.get(f"{API}/api/models").mock(
        return_value=httpx.Response(200, json=[{"id": "google/veo-3.1", "kind": "video"}])
    )

    models = await tool_list_models(api, kind="video")

    assert models[0]["id"] == "google/veo-3.1"
    assert route.calls.last.request.url.params["kind"] == "video"


@respx.mock
async def test_list_models_without_a_kind_sends_no_filter(api):
    route = respx.get(f"{API}/api/models").mock(return_value=httpx.Response(200, json=[]))

    await tool_list_models(api)

    assert "kind" not in route.calls.last.request.url.params


@respx.mock
async def test_generate_image_posts_the_declared_fields(api):
    route = respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(200, json=IMAGE_GENERATION)
    )

    await tool_generate_image(
        api,
        model="openai/gpt-image-2",
        prompt="neon city",
        project="art",
        aspect_ratio="16:9",
        quality="high",
        seed=7,
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["model"] == "openai/gpt-image-2"
    assert sent["project"] == "art"
    assert sent["aspect_ratio"] == "16:9"
    assert sent["quality"] == "high"
    assert sent["seed"] == 7


@respx.mock
async def test_generate_image_omits_unset_parameters(api):
    route = respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(200, json=IMAGE_GENERATION)
    )

    await tool_generate_image(api, model="a/b", prompt="x")

    sent = json.loads(route.calls.last.request.read())
    assert set(sent) == {"model", "prompt", "project", "input_reference_asset_ids"}
    assert sent["input_reference_asset_ids"] == []


@respx.mock
async def test_generate_image_rejects_a_batch_before_any_http_call(api):
    # Spec section 5.5. Rejecting locally means a mistaken n never reaches a
    # billable endpoint at all.
    route = respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(200, json=IMAGE_GENERATION)
    )

    with pytest.raises(ToolError) as caught:
        await tool_generate_image(api, model="a/b", prompt="x", n=4)

    assert caught.value.code == "batch_not_supported"
    assert route.call_count == 0


@respx.mock
async def test_generate_image_returns_both_a_local_path_and_a_url(api):
    # Spec section 6.2: agents run on the same host and need both.
    respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(200, json=IMAGE_GENERATION)
    )

    result = await tool_generate_image(api, model="a/b", prompt="x")

    assert result["asset"]["local_path"].startswith("/")
    assert result["asset"]["url"] == f"{API}/media/projects/unsorted/images/a.png"


@respx.mock
async def test_generate_video_returns_the_job_id_without_waiting(api):
    # Spec section 6.2: a multi-minute render inside one tool call invites
    # client timeouts, so exactly one request is made.
    submit = respx.post(f"{API}/api/generate/video").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "b7e004aa1c32",
                "kind": "video",
                "state": "SUBMITTED",
                "provider_job_id": "or-job-1",
                "asset": None,
                "cost_usd": None,
                "cost_known": False,
            },
        )
    )
    poll = respx.get(f"{API}/api/jobs/b7e004aa1c32").mock(
        return_value=httpx.Response(200, json={"id": "b7e004aa1c32", "state": "COMPLETE"})
    )

    result = await tool_generate_video(api, model="google/veo-3.1", prompt="a beach")

    assert result["id"] == "b7e004aa1c32"
    assert result["state"] == "SUBMITTED"
    assert submit.call_count == 1
    assert poll.call_count == 0


@respx.mock
async def test_generate_video_forwards_frame_assets(api):
    route = respx.post(f"{API}/api/generate/video").mock(
        return_value=httpx.Response(202, json={"id": "b", "state": "SUBMITTED", "asset": None})
    )

    await tool_generate_video(
        api,
        model="kwaivgi/kling-v3.0-pro",
        prompt="pan",
        duration=5,
        generate_audio=True,
        first_frame_asset_id="0c118b4e77aa",
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["first_frame_asset_id"] == "0c118b4e77aa"
    assert sent["generate_audio"] is True
    assert sent["duration"] == 5
    assert "last_frame_asset_id" not in sent


@respx.mock
async def test_generate_video_reports_an_unknown_cost_as_null_not_zero(api):
    # Spec section 3.4: zero would let a spend cap silently never trip.
    respx.post(f"{API}/api/generate/video").mock(
        return_value=httpx.Response(
            202, json={"id": "b", "state": "SUBMITTED", "asset": None, "cost_usd": None}
        )
    )

    result = await tool_generate_video(api, model="a/b", prompt="x")

    assert result["cost_usd"] is None


@respx.mock
async def test_get_job_passes_the_long_poll_bound(api):
    route = respx.get(f"{API}/api/jobs/b7e004aa1c32").mock(
        return_value=httpx.Response(
            200, json={"id": "b7e004aa1c32", "state": "RUNNING", "asset": None}
        )
    )

    await tool_get_job(api, generation_id="b7e004aa1c32", wait_seconds=30)

    assert route.calls.last.request.url.params["wait_seconds"] == "30"


@respx.mock
async def test_get_job_defaults_to_returning_immediately(api):
    route = respx.get(f"{API}/api/jobs/b").mock(
        return_value=httpx.Response(200, json={"id": "b", "state": "RUNNING", "asset": None})
    )

    await tool_get_job(api, generation_id="b")

    assert route.calls.last.request.url.params["wait_seconds"] == "0"


@respx.mock
async def test_a_cap_rejection_reaches_the_agent_with_its_stable_code(api):
    respx.post(f"{API}/api/generate/video").mock(
        return_value=httpx.Response(
            402, json={"error": "local_daily_cap", "message": "cap reached"}
        )
    )

    with pytest.raises(ToolError) as caught:
        await dispatch(api, "generate_video", {"model": "a/b", "prompt": "x"})

    assert caught.value.code == "local_daily_cap"


async def test_an_unknown_tool_name_is_a_tool_error(api):
    with pytest.raises(ToolError) as caught:
        await dispatch(api, "delete_everything", {})

    assert caught.value.code == "internal_error"
    assert "delete_everything" in caught.value.message


async def test_a_missing_required_argument_is_a_validation_error(api):
    with pytest.raises(ToolError) as caught:
        await dispatch(api, "generate_image", {"prompt": "x"})

    assert caught.value.code == "validation_failed"
    assert "model" in caught.value.message
