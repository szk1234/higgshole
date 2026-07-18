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

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

#: Where the REST API listens when nothing overrides it (spec section 8).
DEFAULT_API_BASE: str = "http://127.0.0.1:8077"

#: Environment variable an agent host sets to point at a non-default deployment.
#: It is read by the MCP *client* process, in the agent host's own environment,
#: to locate an already-running server, so it is deliberately *not* a
#: ``Settings`` field: ``Settings`` configures the web service itself. Default:
#: the loopback address in DEFAULT_API_BASE ("http://127.0.0.1:8077").
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
        parts.append(f"{issue.get('parameter')}={issue.get('value')}: {issue.get('message')}")
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
            "duration": {
                "type": "integer",
                "description": "Seconds; must be a supported value.",
            },
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


async def tool_list_models(api: HiggsHoleAPI, *, kind: str | None = None) -> list[dict[str, Any]]:
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


async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
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
