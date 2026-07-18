import json

import httpx
import mcp.types as types
import respx

from higgshole.mcp_server import handle_call_tool, run, server

API = "http://127.0.0.1:8077"


def test_the_server_is_named_for_the_project():
    assert server.name == "higgshole"


def test_both_tool_handlers_are_registered_on_the_server():
    assert types.ListToolsRequest in server.request_handlers
    assert types.CallToolRequest in server.request_handlers


def test_the_console_entrypoint_is_callable():
    assert callable(run)


@respx.mock
async def test_a_successful_call_returns_one_json_text_block():
    respx.get(f"{API}/api/budget").mock(
        return_value=httpx.Response(200, json={"in_flight": 1, "cap_usd": "5.00"})
    )

    blocks = await handle_call_tool("get_budget", {})

    assert len(blocks) == 1
    assert isinstance(blocks[0], types.TextContent)
    assert json.loads(blocks[0].text)["cap_usd"] == "5.00"


@respx.mock
async def test_an_api_error_is_rendered_as_a_structured_json_block():
    # An agent must be able to branch on the code without parsing prose.
    respx.post(f"{API}/api/generate/image").mock(
        return_value=httpx.Response(
            400, json={"error": "validation_failed", "message": "duration unsupported"}
        )
    )

    blocks = await handle_call_tool("generate_image", {"model": "a/b", "prompt": "x"})

    payload = json.loads(blocks[0].text)
    assert payload["error"] == "validation_failed"
    assert payload["message"] == "duration unsupported"


async def test_a_missing_argument_is_reported_without_any_http_call():
    blocks = await handle_call_tool("generate_image", {"prompt": "x"})

    assert json.loads(blocks[0].text)["error"] == "validation_failed"


async def test_absent_arguments_are_treated_as_an_empty_mapping():
    # A client may send no arguments object at all; that must produce a tool
    # error payload rather than a TypeError on ``**None``.
    blocks = await handle_call_tool("get_media", None)

    assert json.loads(blocks[0].text)["error"] == "validation_failed"


@respx.mock
async def test_the_api_base_is_read_from_the_environment_at_call_time(monkeypatch):
    # An agent host may set HIGGSHOLE_API_BASE after this module is imported,
    # so the base must be resolved per call rather than captured at import.
    monkeypatch.setenv("HIGGSHOLE_API_BASE", "http://127.0.0.1:9999")
    route = respx.get("http://127.0.0.1:9999/api/budget").mock(
        return_value=httpx.Response(200, json={"in_flight": 0})
    )

    await handle_call_tool("get_budget", {})

    assert route.call_count == 1
