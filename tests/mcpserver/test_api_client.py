import httpx
import pytest
import respx

from higgshole.mcp_server import (
    API_BASE_ENV,
    DEFAULT_API_BASE,
    HiggsHoleAPI,
    ToolError,
    resolve_api_base,
)

API = "http://127.0.0.1:8077"


@pytest.fixture
async def api():
    client = HiggsHoleAPI(API)
    try:
        yield client
    finally:
        await client.aclose()


def test_default_api_base_matches_the_documented_bind_defaults():
    # Spec section 8: the service binds 127.0.0.1:8077 unless reconfigured.
    assert DEFAULT_API_BASE == "http://127.0.0.1:8077"
    assert API_BASE_ENV == "HIGGSHOLE_API_BASE"


def test_resolve_api_base_prefers_the_environment_variable():
    assert resolve_api_base({API_BASE_ENV: "http://10.0.0.4:9000"}) == "http://10.0.0.4:9000"


def test_resolve_api_base_falls_back_to_the_default():
    assert resolve_api_base({}) == DEFAULT_API_BASE


def test_resolve_api_base_strips_a_trailing_slash():
    # Paths are joined as "/api/..." so a trailing slash would double it.
    assert resolve_api_base({API_BASE_ENV: "http://host:8077/"}) == "http://host:8077"


@respx.mock
async def test_a_json_object_response_is_returned_as_a_dict(api):
    respx.get(f"{API}/api/budget").mock(
        return_value=httpx.Response(200, json={"cap_usd": "5.00", "in_flight": 0})
    )

    payload = await api.request("GET", "/api/budget")

    assert payload["cap_usd"] == "5.00"


@respx.mock
async def test_a_json_array_response_is_wrapped_under_items(api):
    # GET /api/projects returns a bare array; request() has a dict return type,
    # so arrays are wrapped rather than the signature being widened.
    respx.get(f"{API}/api/projects").mock(
        return_value=httpx.Response(200, json=[{"slug": "unsorted"}])
    )

    payload = await api.request("GET", "/api/projects")

    assert payload["items"] == [{"slug": "unsorted"}]


@respx.mock
async def test_query_parameters_are_forwarded_and_none_values_dropped(api):
    route = respx.get(f"{API}/api/media").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    await api.request("GET", "/api/media", params={"project": "art", "model": None})

    sent = route.calls.last.request.url
    assert sent.params["project"] == "art"
    assert "model" not in sent.params


@respx.mock
async def test_a_json_body_is_posted_verbatim(api):
    import json

    route = respx.post(f"{API}/api/projects").mock(
        return_value=httpx.Response(201, json={"slug": "art"})
    )

    await api.request("POST", "/api/projects", json_body={"name": "Art"})

    assert json.loads(route.calls.last.request.read()) == {"name": "Art"}


@respx.mock
async def test_an_error_body_surfaces_its_stable_code(api):
    # The API's error codes are frozen; an agent branches on them.
    respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(
            402,
            json={"error": "local_daily_cap", "message": "cap of 5.00 reached", "issues": []},
        )
    )

    with pytest.raises(ToolError) as caught:
        await api.request("POST", "/api/generate/image", json_body={})

    assert caught.value.code == "local_daily_cap"
    assert "5.00" in caught.value.message


@respx.mock
async def test_validation_issues_are_folded_into_the_message(api):
    respx.post(f"{API}/api/generate/video").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "validation_failed",
                "message": "request rejected",
                "issues": [
                    {
                        "parameter": "duration",
                        "value": "7",
                        "severity": "hard",
                        "message": "supported: 3, 5, 10",
                    }
                ],
            },
        )
    )

    with pytest.raises(ToolError) as caught:
        await api.request("POST", "/api/generate/video", json_body={})

    assert "duration" in caught.value.message
    assert "3, 5, 10" in caught.value.message


@respx.mock
async def test_a_non_json_error_body_still_raises_a_tool_error(api):
    respx.get(f"{API}/api/models").mock(return_value=httpx.Response(500, text="<html>oops"))

    with pytest.raises(ToolError) as caught:
        await api.request("GET", "/api/models")

    assert caught.value.code == "internal_error"
    assert "500" in caught.value.message


@respx.mock
async def test_a_204_response_returns_an_empty_dict(api):
    respx.delete(f"{API}/api/media/a3f21c9d4e07").mock(return_value=httpx.Response(204))

    assert await api.request("DELETE", "/api/media/a3f21c9d4e07") == {}


@respx.mock
async def test_an_unreachable_api_is_reported_as_such(api):
    # The commonest agent-side failure is the service simply not running.
    respx.get(f"{API}/api/budget").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ToolError) as caught:
        await api.request("GET", "/api/budget")

    assert caught.value.code == "api_unreachable"
    assert API in caught.value.message
