"""HTTP client for the OpenRouter image and video generation APIs.

This module performs no filesystem or database access whatsoever. It returns
bytes and value objects; persisting them belongs to store/. That boundary is
what lets the entire provider integration be tested offline and for free.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx

from .errors import (
    AuthError,
    IndeterminateError,
    ProviderError,
    error_from_response,
)
from .types import ImageModel, ImageResult, KeyStatus, VideoJob, VideoModel

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: OpenRouter keys begin with this prefix followed by a non-empty payload.
_KEY_PREFIX = "sk-or-v1-"


def looks_like_openrouter_key(candidate: str) -> bool:
    """Whether a pasted string has the shape of an OpenRouter key.

    Checked client-side because the server's 401 messages are misleading: a
    key with a foreign prefix yields "Missing Authentication header", the same
    text an empty field produces.
    """
    candidate = candidate.strip()
    return candidate.startswith(_KEY_PREFIX) and len(candidate) > len(_KEY_PREFIX)


def _image_reference(url: str) -> dict:
    """Wrap a URL or data URI in OpenRouter's ContentPartImage envelope."""
    return {"type": "image_url", "image_url": {"url": url}}


def _frame_image(url: str, frame_type: str) -> dict:
    """A ContentPartImage plus the required frame_type discriminator."""
    return {**_image_reference(url), "frame_type": frame_type}


def _without_nones(params: dict[str, Any]) -> dict[str, Any]:
    """Drop unset parameters so the provider applies its own defaults."""
    return {key: value for key, value in params.items() if value is not None}


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        # Fail fast and legibly on a missing key. httpx accepts the resulting
        # "Bearer " header at construction and only rejects it at request time
        # with "Illegal header value", which surfaces to a first-run operator
        # as an inscrutable transport error rather than "set an API key".
        if not api_key or not api_key.strip():
            raise AuthError("No OpenRouter API key configured.")

        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals -------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = None
        raise error_from_response(response.status_code, body)

    async def _get_json(self, path: str) -> Any:
        response = await self._client.get(path)
        self._raise_for_status(response)
        return response.json()

    async def _post_json(self, path: str, payload: dict) -> Any:
        """POST a body, converting post-send transport failures to
        IndeterminateError so callers never blindly retry a possible charge.
        """
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise IndeterminateError(f"timed out after sending: {exc}") from exc
        except httpx.TransportError as exc:
            raise IndeterminateError(f"connection failed after sending: {exc}") from exc

        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _entries(payload: Any) -> list[dict]:
        """Normalise ``{"data": [...]}`` and bare-array response shapes."""
        if isinstance(payload, dict):
            return list(payload.get("data") or [])
        return list(payload or [])

    # -- catalogue -------------------------------------------------------

    async def list_video_models(self) -> tuple[VideoModel, ...]:
        payload = await self._get_json("/videos/models")
        return tuple(VideoModel.from_api(entry) for entry in self._entries(payload))

    async def list_image_models(self) -> tuple[ImageModel, ...]:
        payload = await self._get_json("/images/models")
        return tuple(ImageModel.from_api(entry) for entry in self._entries(payload))

    async def get_image_model_pricing(self, model_id: str) -> list[dict]:
        """Fetch a single image model's pricing line items.

        Image pricing is not present in the catalogue listing; it requires one
        request per model, which is why the caller caches it (spec section 4.2).
        """
        payload = await self._get_json(f"/images/models/{model_id}/endpoints")
        data = payload.get("data") if isinstance(payload, dict) else None
        endpoints = (data or {}).get("endpoints") or []
        if not endpoints:
            return []
        return list(endpoints[0].get("pricing") or [])

    # -- image generation ------------------------------------------------

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        input_references: Sequence[str] = (),
        **params: Any,
    ) -> ImageResult:
        """Generate one image synchronously.

        ``params`` accepts any of the documented optional fields (aspect_ratio,
        resolution, size, quality, output_format, background, seed, ...).
        Unset values are omitted rather than sent as null.
        """
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(_without_nones(params))
        if input_references:
            body["input_references"] = [_image_reference(u) for u in input_references]

        payload = await self._post_json("/images", body)

        entries = payload.get("data") or []
        if not entries:
            raise ProviderError("response contained no image data")

        first = entries[0]
        usage = payload.get("usage") or {}
        cost = usage.get("cost")

        return ImageResult(
            data=base64.b64decode(first["b64_json"]),
            media_type=first.get("media_type") or "image/png",
            cost=None if cost is None else Decimal(str(cost)),
        )

    # -- video generation ------------------------------------------------

    async def submit_video(
        self,
        *,
        model: str,
        prompt: str,
        frame_images: Sequence[tuple[str, str]] = (),
        input_references: Sequence[str] = (),
        **params: Any,
    ) -> VideoJob:
        """Submit an asynchronous video job and return immediately.

        ``frame_images`` items are ``(url, frame_type)`` pairs where frame_type
        is "first_frame" or "last_frame". If both frame_images and
        input_references are supplied the provider honours frame_images and
        ignores the rest, so callers should send only one.

        No callback_url is ever sent: webhooks are out of scope (spec 2.6).
        """
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(_without_nones(params))
        body.pop("callback_url", None)

        if frame_images:
            body["frame_images"] = [_frame_image(u, t) for u, t in frame_images]
        elif input_references:
            body["input_references"] = [_image_reference(u) for u in input_references]

        payload = await self._post_json("/videos", body)
        return VideoJob.from_api(payload)

    async def get_video_job(self, job_id: str) -> VideoJob:
        """Poll a job's current state. Safe to retry — an idempotent GET."""
        payload = await self._get_json(f"/videos/{job_id}")
        return VideoJob.from_api(payload)

    async def download_video(self, job_id: str, *, index: int = 0) -> bytes:
        """Fetch the rendered video.

        Must be called as soon as the job reports completed: OpenRouter streams
        from the upstream provider rather than storing the result, and no
        retention window is published (spec section 2.5).
        """
        response = await self._client.get(
            f"/videos/{job_id}/content", params={"index": index}
        )
        self._raise_for_status(response)
        return response.content

    # -- key ---------------------------------------------------------------

    async def get_key_status(self) -> KeyStatus:
        """Fetch the key's authoritative limit and usage figures.

        Free to call, and the source of truth for remaining budget.
        """
        return KeyStatus.from_api(await self._get_json("/key"))

    async def validate_key(self) -> bool:
        """Whether the configured key authenticates. Costs nothing.

        Returns False only for an authentication failure; every other error
        propagates, so a provider outage is never reported as a bad key.
        """
        try:
            await self.get_key_status()
        except AuthError:
            return False
        return True
