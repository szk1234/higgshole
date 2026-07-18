# HiggsHole Plan 5 — MCP Server & Deployment

> **How to execute this plan:** work through it strictly task by task, in order.
> Each task is self-contained and ends with a passing test suite and a commit,
> so it is a natural review checkpoint — do not start the next task until the
> current one is green. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Every task follows the same cycle: write a failing test, run it to confirm it
> fails for the reason you expect, write the minimal implementation, confirm it
> passes, commit. Do not write implementation before its test.

**Goal:** Expose the eleven agent-facing operations of spec §6.2 as a stdio MCP server that is a pure HTTP translation of the Plan 4 REST API, then ship the project — hardened systemd unit, deployment and MCP registration documentation, a single opt-in live test resolving spec open item §12.1, and an honest README.

**Architecture:** `mcp_server.py` sits outside the package's dependency graph: it imports `config` and `httpx` and nothing else from the project (spec §4.1). Every tool is a keyword-only async function that builds one HTTP request against the Plan 4 API and returns its decoded JSON, so the MCP surface and the REST surface cannot drift apart. Deployment is a placeholder-bearing systemd unit plus two documents; nothing machine-specific is committed.

**Tech Stack:** Python 3.12+, `uv`, `mcp` (the official Model Context Protocol Python SDK), `httpx`, `pytest`, `pytest-asyncio`, `respx`.

**Source specification:** docs/specs/2026-07-18-higgshole-design.md

**Depends on:** Plan 4 (which depends on Plans 1–3)

### Which MCP SDK, exactly

This plan targets the **official Model Context Protocol Python SDK**, published
on PyPI as **`mcp`** (repository `modelcontextprotocol/python-sdk`), pinned as
**`mcp>=1.12,<2.0`**.

Two things about that pin are deliberate and must not be changed casually:

- **The package is `mcp`, not `fastmcp`.** `fastmcp` is a separate third-party
  project; the SDK vendors its own `mcp.server.fastmcp` module. Depending on
  the standalone package would add a second, differently-versioned protocol
  implementation.
- **The upper bound excludes 2.x.** The SDK's 2.x line replaces the
  decorator-based low-level server (`@server.list_tools()`,
  `@server.call_tool()`, handlers returning `list[TextContent]`) with a
  constructor-injected `(ctx, params) -> Result` API. This plan is written
  against the 1.x low-level server because the frozen contract specifies
  `server: Server` with **explicit JSON input schemas per tool**, which the
  low-level server expresses directly. Migrating to 2.x is a deliberate,
  separate change, not something a `uv lock --upgrade` should perform silently.

The low-level server is used rather than `mcp.server.fastmcp.FastMCP` because
FastMCP derives schemas from type annotations. The contract requires explicit,
reviewable JSON Schema for all eleven tools — an agent's only description of
what a paid generation accepts should be written down, not inferred.

## Global Constraints

- **Python 3.12+**, `uv` for dependency management, pytest with `asyncio_mode = "auto"`.
- **Public repository.** No committed file may contain a personal name, an employer name, a machine-specific absolute path, or an API key.
- **`mcp_server.py` contains no business logic.** It imports nothing from `store`, `jobs`, `budget`, `catalog`, `web` or `orclient`. Its only project import is `higgshole.config` (spec §4.1). Every tool is one HTTP call.
- **No test may make a real network request or cost money.** The autouse `_forbid_real_network` fixture from Plan 1 Task 10 is inherited by every new test package. New test directories get an `__init__.py`; none of them adds a second `conftest.py` that overrides the guard. The single live test in Task 8 is marked `@pytest.mark.live` and additionally `skipif`-guarded on `HIGGSHOLE_LIVE_TESTS`.
- **Never fabricate a cost.** Cost fields cross the MCP boundary as JSON strings or `null`, never floats and never `0` for unknown (spec §3.4).
- **Terminal job statuses are exactly** `completed`, `failed`, `cancelled`, `expired`. Anything else is non-terminal — keep polling (spec §2.4). The MCP layer never interprets a status; it forwards the API's `state` field.
- **`generate_video` never blocks.** It returns as soon as the API returns the job ID (spec §6.2). `get_job` performs the waiting, bounded by a caller-supplied `wait_seconds`.
- **Every asset-returning tool returns both `local_path` and `url`** (spec §6.2).
- **`n > 1` is rejected locally**, before any HTTP call (spec §5.5).
- **One uvicorn worker** in the systemd unit, with the reason stated in the unit itself (spec §9).
- Commit after every task. Conventional commit prefixes (`feat:`, `test:`, `docs:`, `chore:`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/higgshole/mcp_server.py` | The stdio MCP server: API wrapper, tool schemas, eleven handlers, entrypoint |
| `deploy/higgshole.service.example` | Hardened systemd unit with `@USER@` / `@INSTALL_DIR@` placeholders |
| `docs/deployment.md` | Service user, state directories, unit installation, local overrides |
| `docs/mcp.md` | Registering the stdio server with an MCP client |
| `tests/mcpserver/__init__.py` | Test package marker |
| `tests/mcpserver/test_api_client.py` | `HiggsHoleAPI` HTTP translation and error mapping |
| `tests/mcpserver/test_tool_schemas.py` | The eleven declared tools and their JSON input schemas |
| `tests/mcpserver/test_tool_dispatch.py` | Model, generation and job tools against a stubbed API |
| `tests/mcpserver/test_asset_tools.py` | Upload, media, project and budget tools |
| `tests/mcpserver/test_server_entrypoint.py` | Server registration and result rendering |
| `tests/deploy/__init__.py` | Test package marker |
| `tests/deploy/test_service_unit.py` | Systemd unit content and hardening assertions |
| `tests/docs/__init__.py` | Test package marker |
| `tests/docs/test_docs.py` | Deployment and MCP documentation assertions |
| `tests/docs/test_readme.py` | README accuracy assertions |
| `tests/live/__init__.py` | Test package marker |
| `tests/live/gating.py` | The opt-in predicate, importable and testable offline |
| `tests/live/test_live_gate.py` | Offline proof that the live test is opt-in |
| `tests/live/test_reference_transport.py` | The one paid test resolving spec §12.1 |

---

## Task 1: The API wrapper and tool errors

**Files:**
- Create: `src/higgshole/mcp_server.py`
- Create: `tests/mcpserver/__init__.py`
- Create: `tests/mcpserver/test_api_client.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing from the package. `httpx` only.
- Produces:
  - `DEFAULT_API_BASE: str = "http://127.0.0.1:8077"`
  - `API_BASE_ENV: str = "HIGGSHOLE_API_BASE"` — an MCP-client-side variable
    (set in the agent host's environment, default `http://127.0.0.1:8077`).
    It is deliberately not a `Settings` field: `Settings` configures the server
    process, this only tells a client where that server already listens.
  - `resolve_api_base(environ: Mapping[str, str] | None = None) -> str`
  - `ToolError(code: str, message: str)` with `.code`, `.message`, `.to_payload() -> dict[str, Any]`
  - `HiggsHoleAPI(base_url: str = DEFAULT_API_BASE, *, timeout: float = 60.0)` with `.base_url`, `async request(method, path, *, json_body=None, params=None, files=None) -> dict[str, Any]`, `async aclose()`

- [ ] **Step 1: Write the failing test**

Create an empty `tests/mcpserver/__init__.py` and `tests/mcpserver/test_api_client.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcpserver/test_api_client.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.mcp_server'`.

- [ ] **Step 3: Implement**

Append `"mcp>=1.12,<2.0"` to the existing runtime dependencies in
`pyproject.toml` and add the console script (both are needed from Task 5 onward;
adding them now keeps the dependency change in one commit).

**The dependency list is cumulative.** Plans 1–4 already added `pillow`, `anyio`,
`fastapi`, `starlette`, `jinja2`, `python-multipart` and `uvicorn`; replacing the
list with a shorter one would make `uv sync` uninstall the web stack. Likewise
`[project.scripts]` already contains the `higgshole` web entrypoint from Plan 4
Task 10 — it is asserted by a test, by `deploy/higgshole.service.example` and by
the README, so it must stay. Both blocks below are the complete expected result
after this edit:

```toml
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pillow>=10.3",
    "anyio>=4.4",
    "fastapi>=0.115",
    "starlette>=0.39",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "uvicorn>=0.30",
    "mcp>=1.12,<2.0",
]

[project.scripts]
higgshole = "higgshole.web.app:run"
higgshole-mcp = "higgshole.mcp_server:run"
```

Create `src/higgshole/mcp_server.py`:

```python
"""stdio MCP server exposing HiggsHole to locally running agents.

This module contains no business logic. Every tool is a translation of its
arguments into exactly one HTTP request against the REST API, so the agent
interface and the browser interface cannot diverge (spec section 4.1). It
imports nothing from store/, jobs/, budget/, catalog/, web/ or orclient/.

Targets the official MCP Python SDK (PyPI package ``mcp``), 1.x line. The
low-level server is used rather than FastMCP because every tool's input schema
is written out explicitly: an agent's only description of what a paid
generation accepts should be reviewable, not inferred from annotations.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

#: Where the REST API listens when nothing overrides it (spec section 8).
DEFAULT_API_BASE: str = "http://127.0.0.1:8077"

#: Environment variable an agent host sets to point at a non-default deployment.
#: It is read by the MCP *client* process, in the agent host's own environment,
#: to locate an already-running server, so it is deliberately *not* a
#: ``Settings`` field: ``Settings`` configures the web service itself. It is
#: nevertheless listed in the spec's configuration table and in ``.env.example``
#: (as a commented, clearly-labelled MCP-client entry) so that anyone looking
#: for a HIGGSHOLE_-prefixed variable finds it. Default: the loopback address
#: in DEFAULT_API_BASE ("http://127.0.0.1:8077").
API_BASE_ENV: str = "HIGGSHOLE_API_BASE"


def resolve_api_base(environ: Mapping[str, str] | None = None) -> str:
    """The API base URL, from the environment or the loopback default.

    A trailing slash is stripped because every request path already begins with
    "/api", and httpx would otherwise produce a doubled separator.
    """
    env = os.environ if environ is None else environ
    return (env.get(API_BASE_ENV) or DEFAULT_API_BASE).rstrip("/")


class ToolError(Exception):
    """An API failure rendered for an agent.

    Carries the API's stable machine-readable code alongside the human message
    so a calling agent can branch without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


def _without_nones(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop unset arguments so the API applies its own documented defaults."""
    return {key: value for key, value in values.items() if value is not None}


def _render_issues(issues: Any) -> str:
    if not isinstance(issues, list) or not issues:
        return ""
    parts = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        parts.append(
            f"{issue.get('parameter')}={issue.get('value')}: {issue.get('message')}"
        )
    return "; ".join(parts)


def _tool_error_from(response: httpx.Response) -> ToolError:
    """Turn an error response into a ToolError, preserving the stable code.

    When the body carries no recognisable code the fallback is
    ``internal_error`` rather than a code guessed from the status: inventing a
    code an agent might branch on is worse than admitting ignorance.
    """
    try:
        body = response.json()
    except ValueError:
        body = None

    if not isinstance(body, dict):
        return ToolError(
            "internal_error",
            f"HTTP {response.status_code} from the HiggsHole API with no JSON body",
        )

    code = str(body.get("error") or "internal_error")
    message = str(body.get("message") or f"HTTP {response.status_code}")
    rendered = _render_issues(body.get("issues"))
    if rendered:
        message = f"{message} ({rendered})"
    return ToolError(code, message)


class HiggsHoleAPI:
    """Thin async HTTP wrapper. The only I/O in this module."""

    def __init__(self, base_url: str = DEFAULT_API_BASE, *, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one API call, raising ToolError on any non-2xx response.

        A bare JSON array (returned by the list endpoints) is wrapped as
        ``{"items": [...]}`` so this method keeps a single dict return type;
        callers that expect a list read ``["items"]``.
        """
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                params=None if params is None else _without_nones(params),
                files=files,
            )
        except httpx.HTTPError as exc:
            raise ToolError(
                "api_unreachable",
                f"cannot reach the HiggsHole API at {self._base_url}: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise _tool_error_from(response)

        if response.status_code == 204 or not response.content:
            return {}

        payload = response.json()
        if isinstance(payload, list):
            return {"items": payload}
        if not isinstance(payload, dict):
            return {"value": payload}
        return payload
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv sync --extra dev && uv run pytest tests/mcpserver/test_api_client.py -v`

Expected: PASS — `13 passed`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/higgshole/mcp_server.py tests/mcpserver/
git commit -m "feat: add the MCP server's HTTP wrapper and stable tool errors"
```

---

## Task 2: The eleven tool schemas

**Files:**
- Modify: `src/higgshole/mcp_server.py`
- Create: `tests/mcpserver/test_tool_schemas.py`

**Interfaces:**
- Consumes: `mcp.types.Tool`.
- Produces:
  - `TOOL_NAMES: tuple[str, ...]` — the eleven names in spec §6.2 order
  - `TOOL_SCHEMAS: dict[str, dict[str, Any]]` — JSON Schema per tool
  - `TOOL_DESCRIPTIONS: dict[str, str]`
  - `build_tools() -> list[types.Tool]`
  - `async handle_list_tools() -> list[types.Tool]`

- [ ] **Step 1: Write the failing test**

Create `tests/mcpserver/test_tool_schemas.py`:

```python
import pytest

from higgshole.mcp_server import (
    TOOL_DESCRIPTIONS,
    TOOL_NAMES,
    TOOL_SCHEMAS,
    build_tools,
    handle_list_tools,
)

EXPECTED = {
    "list_models",
    "generate_image",
    "generate_video",
    "get_job",
    "upload_asset",
    "list_media",
    "get_media",
    "delete_media",
    "list_projects",
    "create_project",
    "get_budget",
}


def test_exactly_the_eleven_specified_tools_are_declared():
    # Spec section 6.2 enumerates eleven tools; a twelfth would be business
    # logic that the REST API does not also expose.
    assert set(TOOL_NAMES) == EXPECTED
    assert len(TOOL_NAMES) == 11


def test_every_tool_carries_a_description_and_a_schema():
    for name in TOOL_NAMES:
        assert TOOL_DESCRIPTIONS[name].strip(), name
        assert name in TOOL_SCHEMAS, name


def test_every_input_schema_is_a_closed_object_schema():
    for name, schema in TOOL_SCHEMAS.items():
        assert schema["type"] == "object", name
        assert isinstance(schema["properties"], dict), name
        assert schema["additionalProperties"] is False, name


def test_every_required_field_is_also_declared_as_a_property():
    for name, schema in TOOL_SCHEMAS.items():
        for field in schema.get("required", []):
            assert field in schema["properties"], f"{name}.{field}"


def test_generate_image_declares_its_generation_parameters():
    schema = TOOL_SCHEMAS["generate_image"]

    assert schema["required"] == ["model", "prompt"]
    for field in (
        "project",
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "seed",
        "input_reference_asset_ids",
        "n",
    ):
        assert field in schema["properties"], field


def test_generate_image_pins_n_to_one():
    # Spec section 5.5: batch generation is not supported.
    n_schema = TOOL_SCHEMAS["generate_image"]["properties"]["n"]

    assert n_schema["maximum"] == 1
    assert n_schema["default"] == 1


def test_generate_video_declares_both_frame_slots():
    props = TOOL_SCHEMAS["generate_video"]["properties"]

    assert "first_frame_asset_id" in props
    assert "last_frame_asset_id" in props
    assert "generate_audio" in props
    assert "duration" in props


def test_get_job_declares_a_bounded_long_poll():
    props = TOOL_SCHEMAS["get_job"]["properties"]

    assert props["wait_seconds"]["default"] == 0
    assert props["wait_seconds"]["minimum"] == 0
    assert props["wait_seconds"]["maximum"] >= 1


def test_list_models_constrains_kind_to_the_two_media_types():
    assert TOOL_SCHEMAS["list_models"]["properties"]["kind"]["enum"] == ["image", "video"]


@pytest.mark.parametrize("name", ["list_projects", "get_budget"])
def test_argument_free_tools_declare_no_properties(name):
    assert TOOL_SCHEMAS[name]["properties"] == {}
    assert TOOL_SCHEMAS[name].get("required", []) == []


async def test_the_list_tools_handler_returns_mcp_tool_objects():
    tools = await handle_list_tools()

    assert {tool.name for tool in tools} == EXPECTED
    assert all(tool.inputSchema["type"] == "object" for tool in tools)
    assert tools == build_tools()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcpserver/test_tool_schemas.py -v`

Expected: FAIL — `ImportError: cannot import name 'TOOL_NAMES' from 'higgshole.mcp_server'`.

- [ ] **Step 3: Implement**

Add `import mcp.types as types` to the imports at the top of
`src/higgshole/mcp_server.py`, then append:

```python
#: The eleven tools of spec section 6.2, in specification order.
TOOL_NAMES: tuple[str, ...] = (
    "list_models",
    "generate_image",
    "generate_video",
    "get_job",
    "upload_asset",
    "list_media",
    "get_media",
    "delete_media",
    "list_projects",
    "create_project",
    "get_budget",
)

#: Long-poll ceiling for get_job. Bounded here as well as by the caller so that
#: a mistaken wait_seconds cannot hold an agent's tool call open indefinitely.
MAX_WAIT_SECONDS: int = 300

_PROJECT_PROPERTY = {
    "type": "string",
    "description": "Project slug. Defaults to 'unsorted', which always exists.",
    "default": "unsorted",
}

_ASSET_ID_LIST = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Asset IDs previously returned by upload_asset, get_media or a "
        "generation tool, used as image references."
    ),
    "default": [],
}


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """A closed JSON Schema object.

    additionalProperties is false so an agent that misspells a parameter is
    told so by its own client rather than having the value silently dropped on
    a request that then costs money.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_models": _object_schema(
        {
            "kind": {
                "type": "string",
                "enum": ["image", "video"],
                "description": "Restrict the listing to one media kind.",
            }
        }
    ),
    "generate_image": _object_schema(
        {
            "model": {"type": "string", "description": "Model ID from list_models."},
            "prompt": {"type": "string", "description": "Passed to the provider verbatim."},
            "project": _PROJECT_PROPERTY,
            "aspect_ratio": {"type": "string"},
            "resolution": {"type": "string", "enum": ["512", "1K", "2K", "4K"]},
            "size": {
                "type": "string",
                "description": (
                    "Explicit pixel dimensions, e.g. '1920x1080'. Authoritative: "
                    "a conflicting resolution or aspect_ratio is rejected."
                ),
            },
            "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
            "output_format": {"type": "string", "enum": ["png", "jpeg", "webp", "svg"]},
            "seed": {"type": "integer"},
            "input_reference_asset_ids": _ASSET_ID_LIST,
            "n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1,
                "default": 1,
                "description": "Fixed at 1; batch generation is not supported.",
            },
        },
        ["model", "prompt"],
    ),
    "generate_video": _object_schema(
        {
            "model": {"type": "string", "description": "Model ID from list_models."},
            "prompt": {"type": "string", "description": "Passed to the provider verbatim."},
            "project": _PROJECT_PROPERTY,
            "duration": {"type": "integer", "description": "Seconds; must be a supported value."},
            "resolution": {"type": "string"},
            "aspect_ratio": {"type": "string"},
            "generate_audio": {"type": "boolean"},
            "seed": {"type": "integer"},
            "first_frame_asset_id": {"type": "string"},
            "last_frame_asset_id": {"type": "string"},
            "input_reference_asset_ids": _ASSET_ID_LIST,
        },
        ["model", "prompt"],
    ),
    "get_job": _object_schema(
        {
            "generation_id": {"type": "string"},
            "wait_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_WAIT_SECONDS,
                "default": 0,
                "description": (
                    "Long-poll for up to this many seconds. 0 returns the "
                    "current state immediately."
                ),
            },
        },
        ["generation_id"],
    ),
    "upload_asset": _object_schema(
        {
            "path": {
                "type": "string",
                "description": "Absolute or user-relative path to a local file on this host.",
            },
            "project": _PROJECT_PROPERTY,
        },
        ["path"],
    ),
    "list_media": _object_schema(
        {
            "project": {"type": "string"},
            "kind": {"type": "string", "enum": ["image", "video"]},
            "model": {"type": "string"},
            "created_after": {"type": "string", "description": "ISO-8601 UTC, inclusive."},
            "created_before": {"type": "string", "description": "ISO-8601 UTC, exclusive."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        }
    ),
    "get_media": _object_schema({"generation_id": {"type": "string"}}, ["generation_id"]),
    "delete_media": _object_schema({"generation_id": {"type": "string"}}, ["generation_id"]),
    "list_projects": _object_schema({}),
    "create_project": _object_schema({"name": {"type": "string"}}, ["name"]),
    "get_budget": _object_schema({}),
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_models": (
        "List available image and video models with their capability "
        "constraints. Read this before generating: supported resolutions, "
        "durations, aspect ratios and reference-image slots differ per model."
    ),
    "generate_image": (
        "Generate one image synchronously and return the finished asset with "
        "both its local filesystem path and its HTTP URL. Blocks until the "
        "image exists. Batch generation is not supported."
    ),
    "generate_video": (
        "Submit a video generation job and return its ID immediately. Does "
        "NOT wait for the render; poll with get_job."
    ),
    "get_job": (
        "Fetch a generation's current state, optionally long-polling for up "
        "to wait_seconds. Returns the finished asset once the state is "
        "COMPLETE."
    ),
    "upload_asset": (
        "Ingest a local file into a project's uploads directory and return an "
        "asset ID usable as an image reference or a video frame."
    ),
    "list_media": "Browse the library with optional project, kind, model and date filters.",
    "get_media": "Full metadata for one generation, including its input lineage.",
    "delete_media": "Delete a generation together with its files and thumbnails.",
    "list_projects": "List projects. 'unsorted' always exists.",
    "create_project": "Create a project and return its slug.",
    "get_budget": (
        "Provider-authoritative remaining credit plus local daily-cap status. "
        "Amounts are strings or null; null means the cost is unknown, never zero."
    ),
}


def build_tools() -> list[types.Tool]:
    """The declared tool list, in specification order."""
    return [
        types.Tool(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            inputSchema=TOOL_SCHEMAS[name],
        )
        for name in TOOL_NAMES
    ]


async def handle_list_tools() -> list[types.Tool]:
    return build_tools()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcpserver/test_tool_schemas.py -v`

Expected: PASS — `12 passed` (ten plain tests plus the argument-free-tools test parametrized over two names).

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/mcp_server.py tests/mcpserver/test_tool_schemas.py
git commit -m "feat: declare the eleven MCP tools with explicit input schemas"
```

---

## Task 3: Model, generation and job tools

**Files:**
- Modify: `src/higgshole/mcp_server.py`
- Create: `tests/mcpserver/test_tool_dispatch.py`

**Interfaces:**
- Consumes: `HiggsHoleAPI.request`, `ToolError`.
- Produces:
  - `async tool_list_models(api: HiggsHoleAPI, *, kind: str | None = None) -> list[dict[str, Any]]`
  - `async tool_generate_image(api, *, model, prompt, project="unsorted", aspect_ratio=None, resolution=None, size=None, quality=None, output_format=None, seed=None, input_reference_asset_ids=None, n=1) -> dict[str, Any]`
  - `async tool_generate_video(api, *, model, prompt, project="unsorted", duration=None, resolution=None, aspect_ratio=None, generate_audio=None, seed=None, first_frame_asset_id=None, last_frame_asset_id=None, input_reference_asset_ids=None) -> dict[str, Any]`
  - `async tool_get_job(api, *, generation_id: str, wait_seconds: int = 0) -> dict[str, Any]`
  - `with_local_path(asset: dict[str, Any], base_url: str) -> dict[str, Any]`
  - `async dispatch(api: HiggsHoleAPI, name: str, arguments: dict[str, Any]) -> Any`

- [ ] **Step 1: Write the failing test**

Create `tests/mcpserver/test_tool_dispatch.py`:

```python
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
        return_value=httpx.Response(200, json={"id": "b7e004aa1c32", "state": "RUNNING",
                                               "asset": None})
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcpserver/test_tool_dispatch.py -v`

Expected: FAIL — `ImportError: cannot import name 'dispatch' from 'higgshole.mcp_server'`.

- [ ] **Step 3: Implement**

Append to `src/higgshole/mcp_server.py`:

```python
def with_local_path(asset: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Guarantee an asset carries both a local path and an HTTP URL.

    Agents run on the same host as the service (spec section 6.2), so both
    forms are useful: the path for direct reads, the URL for anything that
    speaks HTTP. The API already returns both; this asserts their presence
    rather than computing them, and absolutises the relative URL so the agent
    does not have to know the base.
    """
    for field in ("local_path", "url"):
        if not asset.get(field):
            raise ToolError(
                "internal_error",
                f"the API returned an asset without a {field}; this is a server bug",
            )

    url = str(asset["url"])
    if url.startswith("/"):
        asset = {**asset, "url": f"{base_url}{url}"}
    return asset


def _with_asset_urls(generation: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Apply with_local_path to a generation's asset when it has one.

    A generation in flight has ``asset: null``; that is not an error, so the
    assertion applies only once an asset exists.
    """
    asset = generation.get("asset")
    if not isinstance(asset, dict):
        return generation
    return {**generation, "asset": with_local_path(asset, base_url)}


async def tool_list_models(
    api: HiggsHoleAPI, *, kind: str | None = None
) -> list[dict[str, Any]]:
    payload = await api.request("GET", "/api/models", params={"kind": kind})
    return list(payload.get("items", []))


async def tool_generate_image(
    api: HiggsHoleAPI,
    *,
    model: str,
    prompt: str,
    project: str = "unsorted",
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    output_format: str | None = None,
    seed: int | None = None,
    input_reference_asset_ids: list[str] | None = None,
    n: int = 1,
) -> dict[str, Any]:
    """Generate one image synchronously.

    ``n`` is rejected here rather than forwarded: refusing locally means a
    mistaken batch request never reaches a billable endpoint (spec section 5.5).
    """
    if n != 1:
        raise ToolError(
            "batch_not_supported",
            f"n is fixed at 1 so that each generation has its own cost record; got {n}",
        )

    body = _without_nones(
        {
            "model": model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "seed": seed,
        }
    )
    body["project"] = project
    body["input_reference_asset_ids"] = list(input_reference_asset_ids or [])

    payload = await api.request("POST", "/api/generate/image", json_body=body)
    return _with_asset_urls(payload, api.base_url)


async def tool_generate_video(
    api: HiggsHoleAPI,
    *,
    model: str,
    prompt: str,
    project: str = "unsorted",
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool | None = None,
    seed: int | None = None,
    first_frame_asset_id: str | None = None,
    last_frame_asset_id: str | None = None,
    input_reference_asset_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a video job and return as soon as the API has its ID.

    Exactly one HTTP request is made. Waiting for a multi-minute render inside
    a single tool call invites client timeouts (spec section 6.2); get_job does
    the waiting, bounded by the caller.
    """
    body = _without_nones(
        {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "generate_audio": generate_audio,
            "seed": seed,
            "first_frame_asset_id": first_frame_asset_id,
            "last_frame_asset_id": last_frame_asset_id,
        }
    )
    body["project"] = project
    body["input_reference_asset_ids"] = list(input_reference_asset_ids or [])

    payload = await api.request("POST", "/api/generate/video", json_body=body)
    return _with_asset_urls(payload, api.base_url)


async def tool_get_job(
    api: HiggsHoleAPI, *, generation_id: str, wait_seconds: int = 0
) -> dict[str, Any]:
    """Current state of one generation, optionally long-polled.

    The wait is performed server-side and bounded by the caller's own value,
    clamped to MAX_WAIT_SECONDS so a mistaken argument cannot pin the
    connection open.
    """
    bounded = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
    payload = await api.request(
        "GET", f"/api/jobs/{generation_id}", params={"wait_seconds": bounded}
    )
    return _with_asset_urls(payload, api.base_url)


async def dispatch(api: HiggsHoleAPI, name: str, arguments: dict[str, Any]) -> Any:
    """Route one tool call to its handler.

    A TypeError from the call is reported as validation_failed: with closed
    input schemas the only way to reach it is a missing or misnamed argument,
    and the handlers themselves perform no arithmetic that could raise one.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ToolError("internal_error", f"unknown tool: {name}")
    try:
        return await handler(api, **arguments)
    except TypeError as exc:
        raise ToolError("validation_failed", f"{name}: {exc}") from exc


TOOL_HANDLERS: dict[str, Any] = {
    "list_models": tool_list_models,
    "generate_image": tool_generate_image,
    "generate_video": tool_generate_video,
    "get_job": tool_get_job,
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcpserver/test_tool_dispatch.py -v`

Expected: PASS — `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/mcp_server.py tests/mcpserver/test_tool_dispatch.py
git commit -m "feat: add model, generation and job MCP tools"
```

---

## Task 4: Upload, library, project and budget tools

**Files:**
- Modify: `src/higgshole/mcp_server.py`
- Create: `tests/mcpserver/test_asset_tools.py`

**Interfaces:**
- Consumes: `HiggsHoleAPI.request`, `with_local_path`, `ToolError`, `TOOL_HANDLERS`.
- Produces:
  - `async tool_upload_asset(api, *, path: str, project: str = "unsorted") -> dict[str, Any]`
  - `async tool_list_media(api, *, project=None, kind=None, model=None, created_after=None, created_before=None, limit=50, offset=0) -> dict[str, Any]`
  - `async tool_get_media(api, *, generation_id: str) -> dict[str, Any]`
  - `async tool_delete_media(api, *, generation_id: str) -> dict[str, Any]`
  - `async tool_list_projects(api) -> list[dict[str, Any]]`
  - `async tool_create_project(api, *, name: str) -> dict[str, Any]`
  - `async tool_get_budget(api) -> dict[str, Any]`
  - `TOOL_HANDLERS` extended to all eleven names

- [ ] **Step 1: Write the failing test**

Create `tests/mcpserver/test_asset_tools.py`:

```python
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
    route = respx.post(f"{API}/api/uploads").mock(
        return_value=httpx.Response(200, json=UPLOAD)
    )

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
                "inputs": [
                    {"asset_id": "0c118b4e77aa", "role": "input_reference", "position": 0}
                ],
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
    route = respx.delete(f"{API}/api/media/a3f21c9d4e07").mock(
        return_value=httpx.Response(204)
    )

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcpserver/test_asset_tools.py -v`

Expected: FAIL — `ImportError: cannot import name 'tool_upload_asset' from 'higgshole.mcp_server'`.

- [ ] **Step 3: Implement**

Add `from pathlib import Path` to the imports at the top of
`src/higgshole/mcp_server.py`, then append:

```python
async def tool_upload_asset(
    api: HiggsHoleAPI, *, path: str, project: str = "unsorted"
) -> dict[str, Any]:
    """Ingest a local file so it can be used as a reference.

    Closes the ingress gap of spec section 6.2: without it an agent holding a
    local image has no way to feed it into a generation. The file is read here
    and posted as multipart; the API owns where it lands on disk.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ToolError("asset_not_found", f"no readable file at {source}")

    files = {
        "file": (source.name, source.read_bytes(), "application/octet-stream"),
        # A (None, value) tuple is httpx's spelling for a plain multipart form
        # field, which is what FastAPI's Form(...) parameter expects.
        "project": (None, project),
    }
    payload = await api.request("POST", "/api/uploads", files=files)
    return with_local_path(payload, api.base_url)


async def tool_list_media(
    api: HiggsHoleAPI,
    *,
    project: str | None = None,
    kind: str | None = None,
    model: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    payload = await api.request(
        "GET",
        "/api/media",
        params={
            "project": project,
            "kind": kind,
            "model": model,
            "created_after": created_after,
            "created_before": created_before,
            "limit": limit,
            "offset": offset,
        },
    )
    items = [_with_asset_urls(item, api.base_url) for item in payload.get("items", [])]
    return {**payload, "items": items}


async def tool_get_media(api: HiggsHoleAPI, *, generation_id: str) -> dict[str, Any]:
    payload = await api.request("GET", f"/api/media/{generation_id}")
    return _with_asset_urls(payload, api.base_url)


async def tool_delete_media(api: HiggsHoleAPI, *, generation_id: str) -> dict[str, Any]:
    """Delete a generation, its files and its thumbnails.

    The API answers 204 with no body, so an explicit confirmation object is
    synthesised: an agent receiving ``{}`` cannot tell success from a no-op.
    """
    await api.request("DELETE", f"/api/media/{generation_id}")
    return {"deleted": True, "generation_id": generation_id}


async def tool_list_projects(api: HiggsHoleAPI) -> list[dict[str, Any]]:
    payload = await api.request("GET", "/api/projects")
    return list(payload.get("items", []))


async def tool_create_project(api: HiggsHoleAPI, *, name: str) -> dict[str, Any]:
    return await api.request("POST", "/api/projects", json_body={"name": name})


async def tool_get_budget(api: HiggsHoleAPI) -> dict[str, Any]:
    """Provider-authoritative credit plus local cap status (spec section 3.2)."""
    return await api.request("GET", "/api/budget")


TOOL_HANDLERS.update(
    {
        "upload_asset": tool_upload_asset,
        "list_media": tool_list_media,
        "get_media": tool_get_media,
        "delete_media": tool_delete_media,
        "list_projects": tool_list_projects,
        "create_project": tool_create_project,
        "get_budget": tool_get_budget,
    }
)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcpserver/test_asset_tools.py -v`

Expected: PASS — `16 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/mcp_server.py tests/mcpserver/test_asset_tools.py
git commit -m "feat: add upload, library, project and budget MCP tools"
```

---

## Task 5: Server registration and the stdio entrypoint

**Files:**
- Modify: `src/higgshole/mcp_server.py`
- Create: `tests/mcpserver/test_server_entrypoint.py`

**Interfaces:**
- Consumes: `mcp.server.lowlevel.Server`, `mcp.server.stdio.stdio_server`, `handle_list_tools`, `dispatch`.
- Produces:
  - `server: Server` — named `"higgshole"`
  - `async handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]`
  - `async main() -> None`
  - `run() -> None` — the console-script entrypoint

- [ ] **Step 1: Write the failing test**

Create `tests/mcpserver/test_server_entrypoint.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcpserver/test_server_entrypoint.py -v`

Expected: FAIL — `ImportError: cannot import name 'handle_call_tool' from 'higgshole.mcp_server'`.

- [ ] **Step 3: Implement**

Add `import asyncio`, `import json`, `from mcp.server.lowlevel import Server`
and `from mcp.server.stdio import stdio_server` to the imports at the top of
`src/higgshole/mcp_server.py`, then append:

```python
#: The MCP server instance. Registration is written as an explicit call below
#: each handler rather than as a decorator purely for readability — the SDK
#: returns the handler unchanged either way, so both forms are equivalent.
server: Server = Server("higgshole")

server.list_tools()(handle_list_tools)


def _json_block(payload: Any) -> list[types.TextContent]:
    """Render a result as one pretty-printed JSON text block.

    ``default=str`` is a backstop only; the API returns money as strings
    already, and no value crossing this boundary should be a float.
    """
    return [
        types.TextContent(
            type="text", text=json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
    ]


async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Execute one tool call.

    A fresh HTTP client is built per call and closed afterwards. A stdio
    server handles few calls across a long-idle session, so a pooled
    connection would more likely be stale than reused.

    Failures are returned as a JSON error object rather than raised: an agent
    can then branch on the stable code, whereas a protocol-level exception
    reaches it only as prose.
    """
    api = HiggsHoleAPI(resolve_api_base())
    try:
        result = await dispatch(api, name, dict(arguments or {}))
    except ToolError as exc:
        return _json_block(exc.to_payload())
    finally:
        await api.aclose()
    return _json_block(result)


server.call_tool()(handle_call_tool)


async def main() -> None:
    """Serve over stdio until the client closes the stream."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    """Console-script entrypoint (``higgshole-mcp``)."""
    asyncio.run(main())
```

> **Note on the registration style:** in SDK 1.x both `Server.call_tool()` and
> `Server.list_tools()` register a wrapper in `server.request_handlers` and
> return the handler *unchanged*, so decorator syntax would behave identically
> and the module-level names would still be the plain functions the tests call.
> The explicit `server.call_tool()(handle_call_tool)` form is used only so the
> handler definitions read as ordinary functions and the registration is a
> visible, separate line; either style is correct.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcpserver/ -v`

Expected: PASS — `63 passed` (api client 13, schemas 12, dispatch 14, asset
tools 16, entrypoint 8).

Run: `uv run pytest tests/mcpserver/test_server_entrypoint.py -v`

Expected: PASS — `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/mcp_server.py tests/mcpserver/test_server_entrypoint.py
git commit -m "feat: register MCP tool handlers and add the stdio entrypoint"
```

---

## Task 6: The systemd unit

**Files:**
- Create: `deploy/higgshole.service.example`
- Create: `tests/deploy/__init__.py`
- Create: `tests/deploy/test_service_unit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `deploy/higgshole.service.example` — a systemd service unit containing only `@USER@` and `@INSTALL_DIR@` placeholders plus non-machine-specific `/var/lib` paths.

- [ ] **Step 1: Write the failing test**

Create an empty `tests/deploy/__init__.py` and `tests/deploy/test_service_unit.py`:

```python
from pathlib import Path

import pytest

UNIT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "higgshole.service.example"

MEDIA_ROOT = "/var/lib/higgshole/media"
STATE_DIR = "/var/lib/higgshole/state"


def _directives() -> dict[str, list[str]]:
    """Parse a unit file into {key: [values]}, tolerating repeated keys."""
    parsed: dict[str, list[str]] = {}
    for raw in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        parsed.setdefault(key.strip(), []).append(value.strip())
    return parsed


def test_the_unit_exists_and_declares_the_three_sections():
    text = UNIT_PATH.read_text(encoding="utf-8")

    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, section


def test_user_and_install_directory_are_placeholders():
    # Spec section 9: no machine-specific values are committed.
    directives = _directives()

    assert directives["User"] == ["@USER@"]
    assert any("@INSTALL_DIR@" in value for value in directives["ExecStart"])
    assert directives["WorkingDirectory"] == ["@INSTALL_DIR@"]


def test_the_environment_file_is_optional():
    # The leading '-' lets the service start before an operator has written
    # /etc/higgshole/higgshole.env, rather than failing at boot.
    assert _directives()["EnvironmentFile"] == ["-/etc/higgshole/higgshole.env"]


@pytest.mark.parametrize(
    ("directive", "value"),
    [
        ("NoNewPrivileges", "yes"),
        ("PrivateTmp", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("ProtectKernelTunables", "yes"),
    ],
)
def test_required_hardening_directives_are_set(directive, value):
    assert _directives()[directive] == [value]


def test_address_families_are_restricted_to_ip_and_unix():
    families = set(_directives()["RestrictAddressFamilies"][0].split())

    assert families == {"AF_INET", "AF_INET6", "AF_UNIX"}


def test_writable_paths_are_limited_to_the_media_root_and_state_directory():
    # ProtectSystem=strict makes everything read-only; these two are the only
    # places the service legitimately writes (spec sections 5.1, 5.2).
    writable = set(_directives()["ReadWritePaths"][0].split())

    assert writable == {MEDIA_ROOT, STATE_DIR}


def test_media_root_and_database_path_are_configured_to_those_paths():
    environment = " ".join(_directives()["Environment"])

    assert f"HIGGSHOLE_MEDIA_ROOT={MEDIA_ROOT}" in environment
    assert f"HIGGSHOLE_DB_PATH={STATE_DIR}/higgshole.db" in environment


def test_exactly_one_uvicorn_worker_is_requested():
    # Spec section 9: multiple workers would each reattach a poller to the
    # same job at boot and the reservation lock is process-local.
    exec_start = _directives()["ExecStart"][0]

    assert "--workers 1" in exec_start
    assert "uvicorn" in exec_start


def test_the_single_worker_requirement_is_explained_in_the_file():
    text = UNIT_PATH.read_text(encoding="utf-8").lower()

    assert "worker" in text
    assert "poller" in text or "reservation" in text


def test_the_service_restarts_and_waits_for_the_network():
    directives = _directives()

    assert directives["Restart"] == ["always"]
    assert "network-online.target" in " ".join(directives["After"])


def test_no_machine_specific_path_or_identity_is_committed():
    text = UNIT_PATH.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "~/", "sk-or-v1-"):
        assert forbidden not in text, forbidden
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/deploy/test_service_unit.py -v`

Expected: FAIL — `FileNotFoundError: [Errno 2] No such file or directory: '.../deploy/higgshole.service.example'`.

- [ ] **Step 3: Implement**

Create `deploy/higgshole.service.example`:

```ini
# HiggsHole systemd service — example unit.
#
# Copy to /etc/systemd/system/higgshole.service and substitute the two
# placeholders:
#
#   @USER@         the unprivileged service account, e.g. higgshole
#   @INSTALL_DIR@  the checkout directory, e.g. /opt/higgshole
#
#   sed -e 's|@USER@|higgshole|g' -e 's|@INSTALL_DIR@|/opt/higgshole|g' \
#       deploy/higgshole.service.example \
#       | sudo tee /etc/systemd/system/higgshole.service
#
# See docs/deployment.md.

[Unit]
Description=HiggsHole media generation console
Documentation=https://github.com/higgshole/higgshole
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=@USER@
Group=@USER@
WorkingDirectory=@INSTALL_DIR@

# Optional: the leading '-' means the service still starts if the file is
# absent, so a first boot does not fail before the operator has written it.
EnvironmentFile=-/etc/higgshole/higgshole.env

Environment=HIGGSHOLE_MEDIA_ROOT=/var/lib/higgshole/media
Environment=HIGGSHOLE_DB_PATH=/var/lib/higgshole/state/higgshole.db
Environment=HIGGSHOLE_BIND_HOST=127.0.0.1
Environment=HIGGSHOLE_BIND_PORT=8077

# Exactly ONE worker, deliberately. Video pollers are in-process asyncio
# tasks: a second worker would reattach its own poller to the same job at
# boot, downloading the result twice and double-counting the spend. The
# reservation lock that enforces the daily cap is likewise process-local.
ExecStart=@INSTALL_DIR@/.venv/bin/uvicorn \
    --factory higgshole.web.app:create_app \
    --host ${HIGGSHOLE_BIND_HOST} \
    --port ${HIGGSHOLE_BIND_PORT} \
    --workers 1

Restart=always
RestartSec=5

# Hardening (spec section 9).
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes

# ProtectSystem=strict makes the whole filesystem read-only; these are the
# only two locations the service writes to.
ReadWritePaths=/var/lib/higgshole/media /var/lib/higgshole/state

[Install]
WantedBy=multi-user.target
```

> `ExecStart` is continued across lines with trailing backslashes, which
> systemd supports and which the test's parser handles because it reads the
> first `ExecStart=` line — take care that `--workers 1` and `uvicorn` both
> appear on it, or collapse the command onto a single line. **Collapse it onto
> a single line** so the assertion is unambiguous:

```ini
ExecStart=@INSTALL_DIR@/.venv/bin/uvicorn --factory higgshole.web.app:create_app --host ${HIGGSHOLE_BIND_HOST} --port ${HIGGSHOLE_BIND_PORT} --workers 1
```

Use the single-line form in the committed file.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/deploy/test_service_unit.py -v`

Expected: PASS — `15 passed` (ten plain tests plus the hardening test parametrized over five directives).

- [ ] **Step 5: Commit**

```bash
git add deploy/higgshole.service.example tests/deploy/
git commit -m "feat: add a hardened systemd unit with placeholders"
```

---

## Task 7: Deployment and MCP documentation

**Files:**
- Create: `docs/deployment.md`
- Create: `docs/mcp.md`
- Create: `tests/docs/__init__.py`
- Create: `tests/docs/test_docs.py`

**Interfaces:**
- Consumes: `deploy/higgshole.service.example`, `higgshole-mcp` console script.
- Produces: two operator documents containing no machine-specific paths.

- [ ] **Step 1: Write the failing test**

Create an empty `tests/docs/__init__.py` and `tests/docs/test_docs.py`:

```python
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
DEPLOYMENT = DOCS / "deployment.md"
MCP = DOCS / "mcp.md"


def test_deployment_covers_creating_the_service_account():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "useradd" in text
    assert "--system" in text or "-r " in text


def test_deployment_covers_the_media_and_state_directories():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "/var/lib/higgshole/media" in text
    assert "/var/lib/higgshole/state" in text
    assert "install -d" in text or "mkdir -p" in text


def test_deployment_explains_installing_the_unit():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "higgshole.service.example" in text
    assert "@USER@" in text and "@INSTALL_DIR@" in text
    assert "systemctl enable" in text


def test_deployment_documents_local_overrides_via_systemctl_edit():
    # Overrides belong in a drop-in, not in an edited copy of the unit, so an
    # upgrade does not silently discard them.
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "systemctl edit higgshole" in text
    assert "override.conf" in text


def test_deployment_states_the_environment_file_holds_the_api_key():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "/etc/higgshole/higgshole.env" in text
    assert "HIGGSHOLE_OPENROUTER_API_KEY" in text
    assert "chmod 600" in text or "0600" in text


def test_mcp_doc_shows_a_stdio_client_registration_block():
    text = MCP.read_text(encoding="utf-8")

    assert "mcpServers" in text
    assert "higgshole-mcp" in text
    assert '"command"' in text


def test_mcp_doc_names_the_api_base_environment_variable():
    text = MCP.read_text(encoding="utf-8")

    assert "HIGGSHOLE_API_BASE" in text
    assert "http://127.0.0.1:8077" in text


def test_mcp_doc_lists_all_eleven_tools():
    from higgshole.mcp_server import TOOL_NAMES

    text = MCP.read_text(encoding="utf-8")

    for name in TOOL_NAMES:
        assert f"`{name}`" in text, name


def test_mcp_doc_states_that_video_generation_does_not_block():
    text = MCP.read_text(encoding="utf-8").lower()

    assert "generate_video" in text
    assert "does not block" in text or "not block" in text


@pytest.mark.parametrize("path", [DEPLOYMENT, MCP])
def test_no_machine_specific_path_or_key_is_committed(path):
    text = path.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "sk-or-v1-a", "sk-or-v1-0"):
        assert forbidden not in text, f"{path.name}: {forbidden}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/docs/test_docs.py -v`

Expected: FAIL — `FileNotFoundError: [Errno 2] No such file or directory: '.../docs/deployment.md'`.

- [ ] **Step 3: Implement**

Create `docs/deployment.md`:

````markdown
# Deployment

HiggsHole runs anywhere Python 3.12 and ffmpeg run. A systemd unit is provided
for boot-time startup on Linux; nothing in the architecture depends on systemd.

Every path below is an example. Substitute your own; nothing machine-specific
is committed to this repository.

## 1. Install

```bash
sudo git clone https://github.com/higgshole/higgshole.git /opt/higgshole
cd /opt/higgshole
uv sync --no-dev
```

`uv sync` creates `/opt/higgshole/.venv`, which is what the unit's `ExecStart`
refers to.

## 2. Create the service account

An unprivileged system account with no login shell and no home directory:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin higgshole
```

## 3. Create the data and state directories

The media root and the database live in different places on purpose: the media
tree is expected to be exported over a file-sharing protocol, and a database
reachable by remote clients risks corruption through incompatible locking. The
state directory also holds the API keys.

```bash
sudo install -d -o higgshole -g higgshole -m 0755 /var/lib/higgshole/media
sudo install -d -o higgshole -g higgshole -m 0750 /var/lib/higgshole/state
```

These two paths are the only ones the unit lists in `ReadWritePaths`. If you
move either, update the drop-in described in step 6 — `ProtectSystem=strict`
makes everything else read-only, so a mismatch shows up as a permission error
rather than a silent write elsewhere.

## 4. Write the environment file

```bash
sudo install -d -m 0755 /etc/higgshole
sudo tee /etc/higgshole/higgshole.env >/dev/null <<'EOF'
HIGGSHOLE_OPENROUTER_API_KEY=your-openrouter-key-here
HIGGSHOLE_DAILY_CAP_USD=10.00
HIGGSHOLE_MAX_JOB_COST_USD=2.00
EOF
sudo chown root:higgshole /etc/higgshole/higgshole.env
sudo chmod 640 /etc/higgshole/higgshole.env
```

The file must not be world-readable. `chmod 600` with `chown higgshole:` works
equally well; the point is that no other account can read the key.

A local daily cap is only the second line of defence. **Also set a credit limit
on the OpenRouter key itself.** That limit is enforced provider-side and is the
only guard a bug in this application cannot defeat.

`EnvironmentFile=` in the unit carries a leading `-`, so the service still
starts if this file is absent. It will then have no key and every generation
will fail at the provider with an authentication error — which is the intended
behaviour, not a silent success.

## 5. Install the unit

The shipped unit carries two placeholders, `@USER@` and `@INSTALL_DIR@`:

```bash
sed -e 's|@USER@|higgshole|g' \
    -e 's|@INSTALL_DIR@|/opt/higgshole|g' \
    /opt/higgshole/deploy/higgshole.service.example \
    | sudo tee /etc/systemd/system/higgshole.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now higgshole.service
sudo systemctl status higgshole.service
```

Confirm it answers:

```bash
curl -s http://127.0.0.1:8077/api/budget
```

## 6. Local overrides

Do not edit `/etc/systemd/system/higgshole.service` directly — a later
reinstall would discard your changes without saying so. Use a drop-in:

```bash
sudo systemctl edit higgshole.service
```

That opens `/etc/systemd/system/higgshole.service.d/override.conf`. To move the
media root, for example, both the environment variable and the writable-path
allowance must change together:

```ini
[Service]
Environment=HIGGSHOLE_MEDIA_ROOT=/srv/media/higgshole
ReadWritePaths=
ReadWritePaths=/srv/media/higgshole /var/lib/higgshole/state
```

The empty `ReadWritePaths=` line resets the list inherited from the unit;
without it the two lists are merged and the old path stays writable.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart higgshole.service
```

## 7. Exposing it beyond the loopback interface

There is **no authentication**, by design. The service binds `127.0.0.1` unless
you change `HIGGSHOLE_BIND_HOST`. Changing it exposes every generation control
and the whole media library to anyone who can reach the port. Do it only on a
network you trust, and consider a reverse proxy that adds authentication.

## 8. Backups

`rescan` can rebuild the generation index from the sidecar files in the media
tree. It **cannot** rebuild `spend_ledger` or `settings` — the local spend
record and the stored API keys exist nowhere else. Back up
`/var/lib/higgshole/state` as well as the media root.

## 9. Logs

```bash
journalctl -u higgshole.service -f
```
````

Create `docs/mcp.md`:

````markdown
# Using HiggsHole from an AI agent (MCP)

HiggsHole ships an MCP server that speaks stdio and exposes the same eleven
operations the web UI uses. It contains no logic of its own: every tool is one
HTTP call against the running HiggsHole API, so the two interfaces cannot drift
apart.

## Prerequisites

The HiggsHole service must be **running**, because the MCP server is a client
of it. Start it however you like — `systemctl start higgshole.service`, or
directly from a checkout — and confirm:

```bash
curl -s http://127.0.0.1:8077/api/budget
```

If the service is down, every tool returns
`{"error": "api_unreachable", "message": "..."}` rather than hanging.

## Registering the server

Most MCP clients read a JSON configuration file with an `mcpServers` object.
Add an entry that launches the console script:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "higgshole-mcp",
      "args": [],
      "env": {
        "HIGGSHOLE_API_BASE": "http://127.0.0.1:8077"
      }
    }
  }
}
```

If `higgshole-mcp` is not on the client's `PATH` — which is common, since the
client may not inherit your shell environment — give an absolute command
instead:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "/opt/higgshole/.venv/bin/higgshole-mcp",
      "args": [],
      "env": {
        "HIGGSHOLE_API_BASE": "http://127.0.0.1:8077"
      }
    }
  }
}
```

Or run it through `uv` from a checkout:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "uv",
      "args": ["run", "--directory", "/opt/higgshole", "higgshole-mcp"]
    }
  }
}
```

## `HIGGSHOLE_API_BASE`

`HIGGSHOLE_API_BASE` defaults to `http://127.0.0.1:8077` and only needs setting
when the service listens elsewhere.

Despite sharing the `HIGGSHOLE_` prefix with the server's settings, it is a
**client-side** variable: you set it in the MCP client's `env` block (or the
shell that launches `higgshole-mcp`), and it only tells the stdio server where
the already-running REST API listens. It is read by the MCP client process, not
by the web service, so it is **not a `Settings` field**. It is still listed in
the configuration table in the spec and in [`.env.example`](../.env.example) —
as a commented, clearly-labelled MCP-client entry — so that it is discoverable
alongside the other `HIGGSHOLE_` variables. Changing where the server listens is
done with the server's own settings, and this value is then pointed at the new
address.

## Verifying it

```bash
HIGGSHOLE_API_BASE=http://127.0.0.1:8077 higgshole-mcp
```

The process will sit waiting on stdin — that is correct; it speaks JSON-RPC
over the stream, not to a terminal. Press Ctrl-D to exit. A crash on startup
means the SDK is missing (`uv sync`) or the console script was not installed.

## The tools

| Tool | Behaviour |
|---|---|
| `list_models` | Image and video models with their capability constraints. Read this first: supported resolutions, durations and reference slots differ per model. |
| `generate_image` | Synchronous. Blocks until the image exists, then returns it. |
| `generate_video` | Submits the job and returns its ID. **Does not block** — a multi-minute render inside one tool call invites client timeouts. |
| `get_job` | Current state, with an optional `wait_seconds` long-poll bounded by the caller. |
| `upload_asset` | Ingests a local file and returns an asset ID usable as a reference or a video frame. |
| `list_media` | Browse the library with project, kind, model and date filters. |
| `get_media` | Full metadata for one generation, including its input lineage. |
| `delete_media` | Removes a generation, its files and its thumbnails. |
| `list_projects` | Enumerate projects. `unsorted` always exists. |
| `create_project` | Create a project. |
| `get_budget` | Provider-authoritative remaining credit plus local cap status. |

## A typical video flow

1. `list_models(kind="video")` — pick a model and read its supported durations
   and resolutions.
2. `upload_asset(path="/path/to/first-frame.png")` — optional, for
   image-to-video.
3. `generate_video(model=..., prompt=..., duration=..., first_frame_asset_id=...)`
   — returns immediately with a generation ID.
4. `get_job(generation_id=..., wait_seconds=60)` — repeat until `state` is
   `COMPLETE` or `FAILED`.

## Conventions worth knowing

- **Both locations are always returned.** Every asset carries `local_path` (a
  filesystem path on this host) and `url` (an absolute HTTP URL). Agents run on
  the same host, so either is usable.
- **Money is a string or `null`, never a number.** `cost_usd: null` with
  `cost_known: false` means the provider reported no cost — it does *not* mean
  the generation was free. Zero is never used to represent an unknown cost.
- **Batch generation is refused.** `n` is fixed at 1; `n > 1` fails locally with
  `batch_not_supported` before any request is sent.
- **Errors are structured.** A failure returns
  `{"error": "<code>", "message": "..."}` with a stable machine-readable code.
  The ones worth branching on: `validation_failed`, `local_daily_cap`,
  `in_flight_limit`, `provider_credit_limit`, `moderation_refused`,
  `indeterminate`, `api_unreachable`.
- **`indeterminate` means a charge may have occurred.** The connection was lost
  after the request was sent. Do not retry blindly.
````

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/docs/test_docs.py -v`

Expected: PASS — `11 passed` (nine plain tests plus the last one parametrized over the two documents).

- [ ] **Step 5: Commit**

```bash
git add docs/deployment.md docs/mcp.md tests/docs/
git commit -m "docs: add deployment and MCP client registration guides"
```

---

## Task 8: Resolve spec open item §12.1 with one live generation

**Files:**
- Create: `tests/live/__init__.py`
- Create: `tests/live/gating.py`
- Create: `tests/live/test_live_gate.py`
- Create: `tests/live/test_reference_transport.py`
- Modify: `docs/specs/2026-07-18-higgshole-design.md`

**Interfaces:**
- Consumes: `higgshole.config.get_settings`, `higgshole.orclient.OpenRouterClient`, `higgshole.orclient.types.VideoModel`, `higgshole.orclient.is_terminal`.
- Produces:
  - `LIVE_TESTS_ENV: str`, `live_tests_enabled(environ=None) -> bool` in `tests/live/gating.py`
  - One `@pytest.mark.live` test answering: **do video providers accept a base64 data URI as a `frame_images` URL?**

### Cost of this task, stated plainly

This is the **only** test in the repository that spends money. It performs one
video generation at the shortest supported duration on the cheapest model in
the live catalogue that accepts a `first_frame` reference.

At the pricing observed on 2026-07-18 the cheapest such model prices around
**$0.03–$0.10 per second of output**, so the shortest job (3–5 seconds) costs
roughly **$0.10–$0.50**. The test refuses to submit if its own pre-flight
estimate exceeds `LIVE_COST_CEILING_USD` (`$1.00`), and it never runs unless
`HIGGSHOLE_LIVE_TESTS` is set. Run it once, record the answer, and do not run
it again.

- [ ] **Step 1: Write the failing test**

Create an empty `tests/live/__init__.py`.

Create `tests/live/gating.py`:

```python
"""The opt-in predicate for the one paid test in this repository.

It lives in its own module so the gate itself can be asserted offline, without
importing (and therefore without risking running) the live test.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

LIVE_TESTS_ENV = "HIGGSHOLE_LIVE_TESTS"


def live_tests_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the operator has opted in to billable tests.

    Any non-empty value enables them; an unset or empty variable does not.
    """
    env = os.environ if environ is None else environ
    return bool(env.get(LIVE_TESTS_ENV, "").strip())
```

Create `tests/live/test_live_gate.py`:

```python
from tests.live.gating import LIVE_TESTS_ENV, live_tests_enabled


def test_the_gate_is_named_as_the_specification_documents_it():
    # Spec section 8 lists HIGGSHOLE_LIVE_TESTS as the opt-in flag.
    assert LIVE_TESTS_ENV == "HIGGSHOLE_LIVE_TESTS"


def test_billable_tests_are_off_unless_explicitly_enabled():
    assert live_tests_enabled({}) is False
    assert live_tests_enabled({LIVE_TESTS_ENV: ""}) is False
    assert live_tests_enabled({LIVE_TESTS_ENV: "   "}) is False


def test_any_non_empty_value_enables_them():
    assert live_tests_enabled({LIVE_TESTS_ENV: "1"}) is True
    assert live_tests_enabled({LIVE_TESTS_ENV: "yes"}) is True
```

Create `tests/live/test_reference_transport.py`:

```python
"""Resolves spec open item 12.1.

Question: do video providers accept a base64 data URI in ``frame_images``?

The OpenAPI schema's ``url`` field is an unconstrained string, so schema-level
acceptance is near-certain; runtime acceptance is what is unknown. This test
answers it with one cheap generation, because the alternative — a public-URL
transport — needs a tunnel or object store and contradicts the trusted-network
premise (spec section 2.8).

COSTS MONEY. Requires HIGGSHOLE_LIVE_TESTS and a funded OpenRouter key.
"""

from __future__ import annotations

import anyio
import base64
import io
import pytest
from decimal import Decimal

from higgshole.config import get_settings
from higgshole.orclient import OpenRouterClient, VideoModel, is_terminal
from tests.live.gating import live_tests_enabled

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not live_tests_enabled(),
        reason="billable; set HIGGSHOLE_LIVE_TESTS=1 to run",
    ),
]

#: Refuse to submit anything estimated above this. A guard against a
#: catalogue change silently turning a $0.30 test into a $30 one.
LIVE_COST_CEILING_USD = Decimal("1.00")

POLL_INTERVAL_S = 10.0
POLL_CEILING_S = 600.0


def _tiny_png_data_uri() -> str:
    """An 8x8 solid PNG as a data URI — small enough to be uncontroversial."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (32, 64, 128)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def _per_second_price(model: VideoModel) -> Decimal | None:
    """The plain per-second price, or None when the model is not so priced.

    Only bare ``duration_seconds`` keys are considered: anything qualified by
    audio, resolution or mode has axes this test does not resolve, and a
    misread axis is exactly the estimation trap of spec section 3.1.
    """
    raw = model.pricing_skus.get("duration_seconds")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return None


def _cheapest_first_frame_model(
    models: tuple[VideoModel, ...],
) -> tuple[VideoModel, int, Decimal]:
    """The cheapest (model, duration, estimated cost) accepting a first frame."""
    candidates: list[tuple[Decimal, VideoModel, int]] = []
    for model in models:
        if "first_frame" not in model.supported_frame_images:
            continue
        price = _per_second_price(model)
        if price is None or not model.supported_durations:
            continue
        duration = min(model.supported_durations)
        candidates.append((price * duration, model, duration))

    if not candidates:
        pytest.skip("no per-second-priced video model accepts a first_frame reference")

    cost, model, duration = min(candidates, key=lambda item: item[0])
    return model, duration, cost


async def test_video_frame_images_accept_a_base64_data_uri():
    settings = get_settings()
    api_key = settings.openrouter_api_key_for("video")
    if not api_key:
        pytest.skip("no video API key configured")

    async with OpenRouterClient(api_key) as client:
        models = await client.list_video_models()
        model, duration, estimate = _cheapest_first_frame_model(models)

        assert estimate <= LIVE_COST_CEILING_USD, (
            f"cheapest candidate {model.id} at {duration}s estimates ${estimate}, "
            f"above the ${LIVE_COST_CEILING_USD} ceiling; refusing to submit"
        )
        print(f"\nLIVE: submitting {model.id} at {duration}s, estimated ${estimate}")

        job = await client.submit_video(
            model=model.id,
            prompt="a slow gentle push in on a plain coloured surface",
            frame_images=[(_tiny_png_data_uri(), "first_frame")],
            duration=duration,
        )
        print(f"LIVE: accepted at submit time, job {job.id}")

        elapsed = 0.0
        while not is_terminal(job.status) and elapsed < POLL_CEILING_S:
            await anyio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
            job = await client.get_video_job(job.id)

        print(
            f"LIVE: terminal status {job.status!r} after {elapsed:.0f}s, "
            f"cost {job.cost}, error {job.error!r}"
        )

    # Submission being accepted proves schema-level acceptance; a `completed`
    # status proves the provider actually fetched and used the data URI. A
    # `failed` status naming the reference is the negative answer, and is
    # recorded in the specification rather than swallowed.
    assert job.status == "completed", (
        f"data URI frame_images did NOT work on {model.id}: status={job.status}, "
        f"error={job.error!r}. Record this in spec section 2.8 and disable video "
        f"reference slots."
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/live/ -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'tests.live.gating'` before the
files exist. After creating them and **without** `HIGGSHOLE_LIVE_TESTS` set,
the same command reports `3 passed, 1 skipped` — the three gate tests pass and
the billable test is skipped, which is the state the repository ships in.

- [ ] **Step 3: Run the live test once, deliberately, and record the answer**

This is the one manual step in the plan, and it is manual because it spends
money. With a funded key configured:

```bash
HIGGSHOLE_LIVE_TESTS=1 uv run pytest tests/live/test_reference_transport.py -v -s
```

Read the printed model ID, duration and estimate before it submits. Then record
the outcome in the specification. Replace the closing paragraphs of §2.8 —
from "**Design response.**" to the end of the section — with the resolved text:

```markdown
**Design response.** Reference transport is a single configurable strategy,
`HIGGSHOLE_REFERENCE_TRANSPORT`, defaulting to `data_uri`.

**Resolved (open item §12.1).** One live generation was performed against the
cheapest per-second-priced video model accepting a `first_frame` reference,
with an 8x8 PNG supplied as a `data:image/png;base64,...` URL.

- Model: `<MODEL ID>`
- Duration: `<N>`s
- Observed cost: `<COST>`
- Result: **`<accepted | rejected>`** — terminal status `<STATUS>`
  <, provider error: `<ERROR>`>

<If accepted:>
Video providers accept base64 data URIs in `frame_images`. Image-to-video
therefore works on a private-network deployment with no further design, and
`jobs.references.video_references_supported()` returns `True` for `DATA_URI`.

<If rejected:>
Video providers do not accept base64 data URIs in `frame_images`.
Image-to-video is therefore **deferred**:
`jobs.references.video_references_supported()` returns `False` for `DATA_URI`
and the UI disables video reference slots with an explanatory message.
Image-to-image is unaffected. A public-URL transport remains out of scope —
making local files provider-reachable requires a tunnel or object store and
contradicts the trusted-network premise.
```

Also mark item 1 of §12 resolved:

```markdown
1. ~~**Verify whether video `frame_images` accepts base64 data URIs** (§2.8).~~
   **Resolved 2026-07-18** — see §2.8. Outcome: `<accepted | rejected>`.
```

If the outcome was *rejected*, `jobs/references.py`'s
`video_references_supported()` must be changed to return `False` for
`DATA_URI`, and its test in Plan 3 updated to match. Do that in this commit.

- [ ] **Step 4: Run to verify the suite is green with the gate closed**

Run: `uv run pytest -q`

Expected: PASS — the whole suite, with `1 skipped` (the billable test). Confirm
the skip reason names the environment variable:

Run: `uv run pytest tests/live/ -v -rs`

Expected: `3 passed, 1 skipped`, reason `billable; set HIGGSHOLE_LIVE_TESTS=1 to run`.

- [ ] **Step 5: Commit**

```bash
git add tests/live/ docs/specs/2026-07-18-higgshole-design.md
git commit -m "test: resolve the video data-URI reference question with one live generation"
```

---

## Task 9: Bring the README up to date

**Files:**
- Modify: `README.md`
- Create: `tests/docs/test_readme.py`

**Interfaces:**
- Consumes: `higgshole.mcp_server.TOOL_NAMES`.
- Produces: a README that describes what exists rather than what is planned.

- [ ] **Step 1: Write the failing test**

Create `tests/docs/test_readme.py`:

```python
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def test_the_readme_no_longer_claims_implementation_has_not_started():
    text = README.read_text(encoding="utf-8")

    assert "implementation not yet started" not in text
    assert "design complete" not in text


def test_the_readme_states_a_running_status():
    text = README.read_text(encoding="utf-8").lower()

    assert "status:" in text
    assert "implemented" in text or "working" in text


def test_the_readme_documents_how_to_run_it():
    text = README.read_text(encoding="utf-8")

    assert "uv sync" in text
    assert "uv run pytest" in text
    assert "127.0.0.1:8077" in text


def test_the_readme_lists_every_mcp_tool():
    from higgshole.mcp_server import TOOL_NAMES

    text = README.read_text(encoding="utf-8")

    for name in TOOL_NAMES:
        assert f"`{name}`" in text, name


def test_the_readme_links_the_deployment_and_mcp_guides():
    text = README.read_text(encoding="utf-8")

    assert "docs/deployment.md" in text
    assert "docs/mcp.md" in text


def test_the_readme_commits_no_machine_specific_path_or_key():
    text = README.read_text(encoding="utf-8")

    for forbidden in ("/home/", "/Users/", "sk-or-v1-a", "sk-or-v1-0"):
        assert forbidden not in text, forbidden
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/docs/test_readme.py -v`

Expected: FAIL — `assert 'implementation not yet started' not in text`, because the README still carries the design-stage status banner.

- [ ] **Step 3: Implement**

Replace `README.md` entirely:

```markdown
# HiggsHole

A self-hosted AI image and video generation console with a browsable media
library, backed by the [OpenRouter](https://openrouter.ai) API and exposed to
local AI agents over MCP.

> **Status:** implemented. Web UI, REST API, MCP server and systemd unit are in
> place and covered by an offline test suite.
> See [the design specification](docs/specs/2026-07-18-higgshole-design.md).

## What it does

- Text-to-image, text-to-video, image-to-image and image-to-video generation
  through a single OpenRouter API key
- Model picker built from OpenRouter's **live capability catalogue** — no
  hardcoded model list, so new models appear without a code change
- Generated media stored on local disk with metadata embedded in the files
  themselves, browsable and playable in the web UI
- Any result can be fed back in as a reference for an edit or improvement pass,
  with the lineage recorded
- An MCP server (stdio) exposing the same functionality to locally running AI
  agents
- Spend controls: provider-enforced key limits, a local ledger of actual costs,
  and a daily cap

## What it deliberately does not do

- No prompt rewriting — prompts are passed to the provider verbatim
- No authentication — intended for a trusted LAN
- No multi-user support
- No batch generation — `n` is fixed at 1 so every generation has its own cost
  record

## Quick start

```bash
uv sync
uv run pytest -q          # the whole suite, offline and free
uv run higgshole          # serves http://127.0.0.1:8077
```

The suite makes no network requests and costs nothing: a socket-blocking
fixture enforces that rather than trusting convention. The single billable test
is opt-in behind `HIGGSHOLE_LIVE_TESTS` and skipped by default.

## MCP tools

Eleven tools, each a thin translation of one REST call — see
[docs/mcp.md](docs/mcp.md) for client registration:

| Tool | Behaviour |
|---|---|
| `list_models` | Image and video models with capability constraints |
| `generate_image` | Synchronous; returns the finished asset |
| `generate_video` | Returns a job ID immediately — does not block |
| `get_job` | Status, with optional bounded long-polling |
| `upload_asset` | Ingests a local file, returning a reusable asset ID |
| `list_media` | Browse with filters |
| `get_media` | Full metadata for one item, including lineage |
| `delete_media` | Removes a generation, its files and its thumbnails |
| `list_projects` | Enumerate projects |
| `create_project` | Create a project |
| `get_budget` | Provider-authoritative credit plus local cap status |

Every asset-returning tool provides both the local filesystem path and the HTTP
URL, since agents run on the same host. Costs are strings or `null` — never `0`
to represent an unknown cost.

## Design notes

The specification records several **verified corrections** to OpenRouter's
published documentation, including the true job-status enumeration, the
unreliability of client-side video cost estimation, and the ephemerality of
result URLs. These were established against the live API and its OpenAPI
specification rather than the prose docs, which contradict it in places.

## Requirements

- Python 3.12+
- `ffmpeg` / `ffprobe`
- An OpenRouter API key

Runs anywhere Python and ffmpeg run. A systemd unit is provided for boot-time
startup on Linux — see [docs/deployment.md](docs/deployment.md) — but nothing
in the architecture depends on it.

## Configuration

Everything is configured through environment variables, readable from a `.env`
file — see [`.env.example`](.env.example) for the full list with defaults. By
default the app writes under `${XDG_DATA_HOME:-~/.local/share}/higgshole` and
`${XDG_STATE_HOME:-~/.local/state}/higgshole` and binds to `127.0.0.1`, so a
fresh clone runs unprivileged with no setup.

Exposing it to your local network is a deliberate act: change
`HIGGSHOLE_BIND_HOST`. There is no authentication, by design.

If you set a daily spend cap, also set a **credit limit on the OpenRouter key
itself**. That limit is enforced provider-side and is the only guard that
cannot be defeated by a bug in this application.

## Licence

MIT
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/docs/ -v && uv run pytest -q && uv run ruff check .`

Expected: PASS — `17 passed` for `tests/docs/` (11 from `test_docs.py`, 6 from
`test_readme.py`), the full suite green with
`1 skipped`, and `All checks passed!` from ruff.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/docs/test_readme.py
git commit -m "docs: describe the implemented system in the README"
```

---

## Definition of done

- [ ] `uv run pytest -q` passes with no network access and `1 skipped` (the billable test)
- [ ] `uv run ruff check .` is clean
- [ ] `mcp_server.py` imports nothing from `store`, `jobs`, `budget`, `catalog`, `web` or `orclient`; verify with `grep -nE "from (higgshole\.)?(store|jobs|budget|catalog|web|orclient)" src/higgshole/mcp_server.py` returning nothing
- [ ] All eleven tools of spec §6.2 are declared, each with an explicit closed JSON input schema, and each has a handler
- [ ] `generate_video` makes exactly one HTTP request and never polls
- [ ] `get_job` forwards a caller-bounded `wait_seconds`, clamped to `MAX_WAIT_SECONDS`
- [ ] `generate_image` rejects `n > 1` before any HTTP call
- [ ] Every asset-returning tool returns both `local_path` and `url`
- [ ] No tool emits `0` for an unknown cost, and no cost crosses the boundary as a float
- [ ] `deploy/higgshole.service.example` carries only `@USER@` and `@INSTALL_DIR@` placeholders, sets every hardening directive of spec §9, limits `ReadWritePaths` to the media root and state directory, and requests exactly one uvicorn worker with the reason stated in the file
- [ ] `docs/deployment.md` covers the service account, both directories, the environment file's permissions, unit installation and `systemctl edit` overrides
- [ ] `docs/mcp.md` shows a working `mcpServers` registration block and names `HIGGSHOLE_API_BASE`
- [ ] Spec open item §12.1 is resolved in `docs/specs/2026-07-18-higgshole-design.md` with the model, duration, cost and outcome recorded
- [ ] `README.md` describes the implemented system, lists all eleven tools, and links both guides
- [ ] No committed file contains a personal name, an employer name, a machine-specific absolute path, or an API key
- [ ] CI passes

---

## Contract additions

The frozen contract's §11 did not cover the following, which this plan adds in
the most consistent style available. Each is confined to `mcp_server.py` and
none of them changes a symbol another plan owns.

| Addition | Reason |
|---|---|
| `resolve_api_base(environ: Mapping[str, str] \| None = None) -> str` | The contract names `DEFAULT_API_BASE` and `API_BASE_ENV` but no function combining them. Resolution is testable only if it takes an explicit mapping. |
| `ToolError.to_payload() -> dict[str, Any]` | `handle_call_tool` must render an error as JSON for the agent; a method keeps the shape in one place. |
| `HiggsHoleAPI.base_url` property | `with_local_path` needs the base to absolutise a relative media URL. |
| Error code `api_unreachable` | The contract's frozen code list describes API responses. A transport failure produces no response, and the commonest agent-side failure — the service not running — needs a code an agent can branch on. It is a client-side code and is documented as such in `docs/mcp.md`. |
| `TOOL_NAMES`, `TOOL_SCHEMAS`, `TOOL_DESCRIPTIONS`, `build_tools()`, `handle_list_tools()`, `handle_call_tool()`, `dispatch()`, `TOOL_HANDLERS`, `MAX_WAIT_SECONDS`, `tool_*` handler functions | The contract specifies "eleven tools, each registered with an explicit JSON input schema" without naming the symbols that carry them. These are those symbols. |
| `with_local_path` absolutises a relative `url` | The contract says it "asserts their presence rather than computing them", which it does; absolutising a path-only URL against the known base is additive and saves every agent from reimplementing it. The assertion behaviour is unchanged. |
| `tool_delete_media` synthesises `{"deleted": true, "generation_id": ...}` | The API answers `204` with no body. An agent receiving `{}` cannot distinguish success from a no-op. |
| `HiggsHoleAPI.request` wraps a bare JSON array as `{"items": [...]}` | The contract fixes the return type as `dict[str, Any]`, but `GET /api/models` and `GET /api/projects` return arrays. Wrapping preserves the frozen signature. |
| `LIVE_COST_CEILING_USD`, `live_tests_enabled()` in `tests/live/gating.py` | Test-only. The spec names `HIGGSHOLE_LIVE_TESTS` but nothing implements the gate; the ceiling is a guard against a catalogue change turning a cheap test into an expensive one. |
```