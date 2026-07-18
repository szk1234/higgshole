"""Frozen value types parsed from OpenRouter responses.

Every ``from_api`` classmethod tolerates missing optional fields, because
the live catalogue is not uniform across models and the OpenAPI schema
marks much of it optional.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

#: Statuses after which no further polling should occur (spec section 2.4).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)


def is_terminal(status: str) -> bool:
    """Whether a provider job status ends the polling loop.

    Unrecognised statuses are deliberately treated as NON-terminal. Polling a
    finished job wastes a few requests and self-corrects at the wall-clock
    ceiling; abandoning a live job loses a paid generation irrecoverably.
    """
    return status in TERMINAL_STATUSES


def _decimal_or_none(value: Any) -> Decimal | None:
    """Convert to Decimal, preserving the difference between absent and zero."""
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class VideoModel:
    id: str
    supported_resolutions: tuple[str, ...] = ()
    supported_aspect_ratios: tuple[str, ...] = ()
    supported_durations: tuple[int, ...] = ()
    supported_sizes: tuple[str, ...] = ()
    supported_frame_images: tuple[str, ...] = ()
    generate_audio: bool | None = None
    seed: bool = False
    pricing_skus: Mapping[str, str] = field(default_factory=dict)
    allowed_passthrough_parameters: tuple[str, ...] = ()

    @property
    def accepts_frame_images(self) -> bool:
        return bool(self.supported_frame_images)

    @classmethod
    def from_api(cls, payload: dict) -> VideoModel:
        return cls(
            id=payload["id"],
            supported_resolutions=tuple(payload.get("supported_resolutions") or ()),
            supported_aspect_ratios=tuple(payload.get("supported_aspect_ratios") or ()),
            supported_durations=tuple(payload.get("supported_durations") or ()),
            supported_sizes=tuple(payload.get("supported_sizes") or ()),
            supported_frame_images=tuple(payload.get("supported_frame_images") or ()),
            generate_audio=payload.get("generate_audio"),
            seed=bool(payload.get("seed")),
            pricing_skus=MappingProxyType(dict(payload.get("pricing_skus") or {})),
            allowed_passthrough_parameters=tuple(
                payload.get("allowed_passthrough_parameters") or ()
            ),
        )


@dataclass(frozen=True)
class ImageModel:
    id: str
    name: str = ""
    max_input_references: int = 0
    quality_values: tuple[str, ...] = ()
    max_n: int = 1
    supports_streaming: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> ImageModel:
        params = payload.get("supported_parameters") or {}

        references = params.get("input_references") or {}
        quality = params.get("quality") or {}
        n_param = params.get("n") or {}

        return cls(
            id=payload["id"],
            name=payload.get("name") or "",
            max_input_references=int(references.get("max", 0)),
            quality_values=tuple(quality.get("values") or ()),
            max_n=int(n_param.get("max", 1)),
            supports_streaming=bool(payload.get("supports_streaming")),
        )


@dataclass(frozen=True)
class ImageResult:
    """One generated image, still in memory. Persisting it is store/'s job."""

    data: bytes
    media_type: str
    cost: Decimal | None


@dataclass(frozen=True)
class VideoJob:
    id: str
    status: str
    generation_id: str | None = None
    result_urls: tuple[str, ...] = ()
    cost: Decimal | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @classmethod
    def from_api(cls, payload: dict) -> VideoJob:
        usage = payload.get("usage") or {}
        return cls(
            id=payload["id"],
            status=payload["status"],
            generation_id=payload.get("generation_id"),
            result_urls=tuple(payload.get("unsigned_urls") or ()),
            cost=_decimal_or_none(usage.get("cost")),
            error=payload.get("error"),
        )


@dataclass(frozen=True)
class KeyStatus:
    """Authoritative budget figures from GET /api/v1/key (spec section 3.2)."""

    limit: Decimal | None = None
    limit_remaining: Decimal | None = None
    limit_reset: str | None = None
    usage: Decimal | None = None
    usage_daily: Decimal | None = None
    is_free_tier: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> KeyStatus:
        data = payload.get("data") or payload
        return cls(
            limit=_decimal_or_none(data.get("limit")),
            limit_remaining=_decimal_or_none(data.get("limit_remaining")),
            limit_reset=data.get("limit_reset"),
            usage=_decimal_or_none(data.get("usage")),
            usage_daily=_decimal_or_none(data.get("usage_daily")),
            is_free_tier=bool(data.get("is_free_tier")),
        )
