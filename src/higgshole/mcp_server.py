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
