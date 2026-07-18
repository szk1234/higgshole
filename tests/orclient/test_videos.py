import json

import httpx
import pytest
import respx

from higgshole.orclient.errors import ProviderError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_submit_returns_the_job_id_immediately(client):
    respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "abc123",
                "status": "pending",
                "polling_url": f"{BASE_URL}/videos/abc123",
            },
        )
    )

    job = await client.submit_video(model="google/veo-3.1", prompt="a beach")

    assert job.id == "abc123"
    assert job.is_terminal is False


@respx.mock
async def test_frame_images_carry_their_frame_type(client):
    route = respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(202, json={"id": "a", "status": "pending"})
    )

    await client.submit_video(
        model="kwaivgi/kling-v3.0-pro",
        prompt="pan across",
        frame_images=[
            ("https://example.com/first.jpg", "first_frame"),
            ("data:image/png;base64,AAAA", "last_frame"),
        ],
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/first.jpg"},
            "frame_type": "first_frame",
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "frame_type": "last_frame",
        },
    ]


@respx.mock
async def test_a_callback_url_is_never_sent(client):
    # Spec section 2.6: webhooks are out of scope; polling only.
    route = respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(202, json={"id": "a", "status": "pending"})
    )

    await client.submit_video(model="a/b", prompt="x", duration=8)

    sent = json.loads(route.calls.last.request.read())
    assert "callback_url" not in sent
    assert sent["duration"] == 8


@respx.mock
@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_non_terminal_statuses_keep_polling(client, status):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": status})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is False


@respx.mock
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "expired"])
async def test_all_four_terminal_statuses_end_polling(client, status):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": status})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is True


@respx.mock
async def test_an_unrecognised_status_does_not_end_polling(client):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": "reticulating"})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is False
    assert job.status == "reticulating"


@respx.mock
async def test_a_failed_job_surfaces_its_error_string(client):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(
            200,
            json={"id": "abc", "status": "failed", "error": "Content policy violation"},
        )
    )

    job = await client.get_video_job("abc")

    assert job.error == "Content policy violation"


@respx.mock
async def test_download_returns_raw_bytes(client):
    respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(200, content=b"\x00\x00\x00 ftypmp42")
    )

    data = await client.download_video("abc")

    assert data.startswith(b"\x00\x00\x00 ftyp")


@respx.mock
async def test_download_passes_the_output_index(client):
    route = respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(200, content=b"x")
    )

    await client.download_video("abc", index=2)

    assert route.calls.last.request.url.params["index"] == "2"


@respx.mock
async def test_a_502_on_download_is_a_provider_error(client):
    # OpenRouter proxies from the upstream provider at download time, so a 502
    # here may mean the provider's retention window has lapsed.
    respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(502, json={"error": {"message": "upstream"}})
    )

    with pytest.raises(ProviderError):
        await client.download_video("abc")
