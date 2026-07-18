import base64
import json

import httpx
import pytest
import respx

from higgshole.orclient.errors import IndeterminateError, ModerationError

BASE_URL = "https://openrouter.ai/api/v1"

PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


def _ok(cost=0.04):
    usage = {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10}
    if cost is not None:
        usage["cost"] = cost
    return httpx.Response(
        200,
        json={
            "created": 1748372400,
            "data": [{"b64_json": PIXEL, "media_type": "image/png"}],
            "usage": usage,
        },
    )


@respx.mock
async def test_generate_image_decodes_the_payload(client):
    respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    result = await client.generate_image(model="a/b", prompt="a cat")

    assert result.data.startswith(b"\x89PNG")
    assert result.media_type == "image/png"
    assert str(result.cost) == "0.04"


@respx.mock
async def test_missing_cost_is_none_rather_than_zero(client):
    # Spec section 3.4: recording zero would let the daily cap never trip.
    respx.post(f"{BASE_URL}/images").mock(return_value=_ok(cost=None))

    result = await client.generate_image(model="a/b", prompt="a cat")

    assert result.cost is None


@respx.mock
async def test_optional_parameters_are_forwarded(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(
        model="a/b", prompt="a cat", aspect_ratio="16:9", quality="high", seed=7
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["aspect_ratio"] == "16:9"
    assert sent["quality"] == "high"
    assert sent["seed"] == 7


@respx.mock
async def test_unset_parameters_are_omitted_entirely(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(model="a/b", prompt="a cat")

    sent = json.loads(route.calls.last.request.read())
    assert set(sent) == {"model", "prompt"}


@respx.mock
async def test_input_references_are_wrapped_in_the_wire_envelope(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(
        model="a/b",
        prompt="make it watercolour",
        input_references=["https://example.com/p.jpg", "data:image/png;base64,AAAA"],
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["input_references"] == [
        {"type": "image_url", "image_url": {"url": "https://example.com/p.jpg"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


@respx.mock
async def test_a_moderation_refusal_raises_its_own_type(client):
    respx.post(f"{BASE_URL}/images").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Content policy violation"}}
        )
    )

    with pytest.raises(ModerationError):
        await client.generate_image(model="a/b", prompt="nope")


@respx.mock
async def test_a_connection_failure_after_submit_is_indeterminate(client):
    # Image generation is synchronous and non-idempotent, so the caller must
    # never silently retry — the charge may already have happened.
    respx.post(f"{BASE_URL}/images").mock(side_effect=httpx.ConnectError("reset"))

    with pytest.raises(IndeterminateError) as caught:
        await client.generate_image(model="a/b", prompt="a cat")

    assert caught.value.may_have_charged is True


@respx.mock
async def test_an_empty_data_array_is_a_provider_error(client):
    respx.post(f"{BASE_URL}/images").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )

    with pytest.raises(Exception, match="no image data"):
        await client.generate_image(model="a/b", prompt="a cat")
