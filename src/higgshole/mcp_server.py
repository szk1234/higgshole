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
import mcp.types as types

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
