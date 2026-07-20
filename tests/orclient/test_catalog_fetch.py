import httpx
import pytest
import respx

from higgshole.orclient.errors import AuthError, RateLimitError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_list_video_models_parses_the_catalogue(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "google/veo-3.1",
                        "supported_durations": [4, 6, 8],
                        "supported_frame_images": ["first_frame", "last_frame"],
                        "pricing_skus": {"duration_seconds_with_audio": "0.40"},
                    },
                    {"id": "openai/sora-2-pro", "supported_durations": [4, 8]},
                ]
            },
        )
    )

    models = await client.list_video_models()

    assert [m.id for m in models] == ["google/veo-3.1", "openai/sora-2-pro"]
    assert models[0].accepts_frame_images is True
    assert models[1].accepts_frame_images is False


@respx.mock
async def test_catalogue_accepts_a_bare_list_response(client):
    # The endpoint has been observed returning a bare array rather than
    # {"data": [...]}, so both shapes must parse.
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(200, json=[{"id": "a/b"}])
    )

    models = await client.list_video_models()

    assert [m.id for m in models] == ["a/b"]


@respx.mock
async def test_list_image_models_parses_reference_limits(client):
    respx.get(f"{BASE_URL}/images/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-image-2",
                        "supported_parameters": {
                            "input_references": {"type": "range", "min": 0, "max": 16}
                        },
                    }
                ]
            },
        )
    )

    models = await client.list_image_models()

    assert models[0].max_input_references == 16


@respx.mock
async def test_image_pricing_is_fetched_per_model(client):
    # The live shape: "endpoints" sits at the top level with no "data"
    # envelope, unlike the catalogue listings. Assuming the envelope made this
    # return [] for every model, silently disabling image cost estimation.
    respx.get(f"{BASE_URL}/images/models/openai/gpt-image-2/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "openai/gpt-image-2",
                "endpoints": [
                    {
                        "provider_name": "OpenAI",
                        "pricing": [
                            {
                                "billable": "output_image",
                                "unit": "token",
                                "cost_usd": 3e-05,
                            }
                        ],
                    }
                ],
            },
        )
    )

    pricing = await client.get_image_model_pricing("openai/gpt-image-2")

    assert pricing[0]["unit"] == "token"


@respx.mock
async def test_image_pricing_also_accepts_a_data_envelope(client):
    """Defensive: the listing endpoints do wrap in "data", so tolerate both."""
    respx.get(f"{BASE_URL}/images/models/a/b/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {"pricing": [{"billable": "output_image", "unit": "image",
                                      "cost_usd": 0.04}]}
                    ]
                }
            },
        )
    )

    pricing = await client.get_image_model_pricing("a/b")

    assert pricing[0]["cost_usd"] == 0.04


@respx.mock
async def test_image_pricing_is_empty_when_a_model_has_no_endpoints(client):
    respx.get(f"{BASE_URL}/images/models/a/b/endpoints").mock(
        return_value=httpx.Response(200, json={"id": "a/b", "endpoints": []})
    )

    assert await client.get_image_model_pricing("a/b") == []


@respx.mock
async def test_the_api_key_is_sent_as_a_bearer_token(client):
    route = respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    await client.list_video_models()

    assert route.calls.last.request.headers["authorization"] == "Bearer sk-or-v1-test"


@respx.mock
async def test_a_401_raises_auth_error(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "User not found.", "code": 401}}
        )
    )

    with pytest.raises(AuthError, match="User not found"):
        await client.list_video_models()


@respx.mock
async def test_a_429_raises_rate_limit_error(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
    )

    with pytest.raises(RateLimitError):
        await client.list_video_models()
