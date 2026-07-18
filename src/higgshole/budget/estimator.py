"""Advisory pre-flight cost estimation.

Spec section 3.1 established that pre-flight estimation is unreliable for
roughly 40-50% of the video catalogue. This module's job is therefore as much
to *refuse* as to compute: every path that cannot resolve to exactly one SKU
returns ``Estimate(amount=None, reason=...)``. A wrong number here would be
displayed as a price and reserved against the daily cap, so a fabricated
figure is worse than no figure at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from higgshole.orclient.types import VideoModel

#: x-ai/grok-imagine-video prefixes its SKU keys with this. Reading '7' as
#: dollars is a 100x overestimate (spec 3.1 item 2).
CENTS_PREFIX: str = "cents_per_"

#: Units with no published conversion table. Always yields an estimate of
#: None — never a guess (spec 3.1 items 1 and 4).
TOKEN_UNITS: frozenset[str] = frozenset({"token", "video_tokens"})

_MODE_PREFIXES = ("text_to_video_", "image_to_video_")
_AUDIO_MARKER = "_with_audio"
_DURATION_MARKER = "duration_seconds"
_RESOLUTION_RE = re.compile(r"^\d+p$")
_CENTS_DIVISOR = Decimal(100)
_QUANTUM = Decimal("0.000001")


class EstimateUnavailable(StrEnum):
    """Machine-readable reasons an estimate cannot be computed (spec 3.2)."""

    TOKEN_PRICED = "token_priced"
    VIDEO_TOKEN_PRICED = "video_token_priced"
    NO_MATCHING_SKU = "no_matching_sku"
    AMBIGUOUS_AXES = "ambiguous_axes"
    MISSING_AXIS = "missing_axis"
    UNKNOWN_UNIT = "unknown_unit"
    NO_PRICING_DATA = "no_pricing_data"


@dataclass(frozen=True)
class Estimate:
    """An advisory pre-flight cost.

    `amount` is None whenever `reason` is set; the two are never both
    populated and never both empty.
    """

    amount: Decimal | None
    reason: EstimateUnavailable | None
    detail: str
    sku_key: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.amount is not None


def _unavailable(reason: EstimateUnavailable, detail: str) -> Estimate:
    return Estimate(amount=None, reason=reason, detail=detail)


def _quantise(value: Decimal) -> Decimal:
    """Six decimal places: sub-cent SKUs are real, sub-microdollar ones are not."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def parse_sku_amount(key: str, raw: str) -> Decimal:
    """Convert one SKU value to USD, dividing by 100 for CENTS_PREFIX keys."""
    value = Decimal(str(raw))
    return value / _CENTS_DIVISOR if key.startswith(CENTS_PREFIX) else value


def _base_key(key: str) -> str:
    return key[len(CENTS_PREFIX) :] if key.startswith(CENTS_PREFIX) else key


def _unit_family(base: str) -> str:
    if "video_tokens" in base or base == "token" or base.endswith("_tokens"):
        return "token"
    if _DURATION_MARKER in base:
        return "duration"
    return "unknown"


def _mode_of(base: str) -> str | None:
    for prefix in _MODE_PREFIXES:
        if base.startswith(prefix):
            return prefix
    return None


def _resolution_of(base: str) -> str | None:
    tail = base.rsplit("_", 1)[-1]
    return tail if _RESOLUTION_RE.match(tail) else None


@dataclass(frozen=True)
class _Match:
    key: str | None
    score: int
    ambiguous: bool


def _best_match(
    keys: list[str],
    skus: dict[str, str],
    *,
    wanted_prefix: str,
    resolution: str | None,
) -> _Match:
    """Pick the most specific SKU whose axes do not contradict the request.

    A key qualified with the *other* mode, or with a different resolution, is
    a contradiction and is discarded. A resolution-qualified key is discarded
    when no resolution was requested, because choosing one arbitrarily would
    invent an axis value the caller never supplied.
    """
    scored: list[tuple[int, str]] = []
    for key in keys:
        base = _base_key(key)
        mode = _mode_of(base)
        found = _resolution_of(base)

        if mode is not None and mode != wanted_prefix:
            continue
        if found is not None and found != resolution:
            continue

        score = (1 if mode == wanted_prefix else 0) + (
            1 if resolution is not None and found == resolution else 0
        )
        scored.append((score, key))

    if not scored:
        return _Match(key=None, score=-1, ambiguous=False)

    top = max(score for score, _ in scored)
    winners = [key for score, key in scored if score == top]
    amounts = {parse_sku_amount(key, skus[key]) for key in winners}
    return _Match(key=winners[0], score=top, ambiguous=len(amounts) > 1)


def estimate_video_cost(
    model: VideoModel,
    *,
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool = False,
    has_frame_images: bool = False,
) -> Estimate:
    """Resolve pricing_skus for the requested axes.

    Returns Estimate(amount=None, reason=...) whenever the axes do not resolve
    to exactly one SKU. In particular, an audio-capable model whose SKU set
    lacks an audio variant yields MISSING_AXIS rather than the non-audio
    price, and a model whose audio SKU is less specific than its non-audio
    SKUs yields AMBIGUOUS_AXES: most-specific-match would silently drop
    Kling's 50% audio surcharge (spec 3.1 item 3).

    `aspect_ratio` is accepted for interface symmetry; no live model prices on
    that axis, so it never participates in SKU selection.
    """
    skus = dict(model.pricing_skus)
    if not skus:
        return _unavailable(
            EstimateUnavailable.NO_PRICING_DATA,
            f"{model.id} publishes no pricing SKUs.",
        )

    families = {key: _unit_family(_base_key(key)) for key in skus}
    duration_keys = [key for key, family in families.items() if family == "duration"]

    if not duration_keys:
        if all(family == "token" for family in families.values()):
            return _unavailable(
                EstimateUnavailable.VIDEO_TOKEN_PRICED,
                f"{model.id} is priced per video token, and no tokens-per-second "
                "table is published, so no cost can be computed before dispatch.",
            )
        return _unavailable(
            EstimateUnavailable.UNKNOWN_UNIT,
            f"{model.id} prices in an unrecognised unit: {', '.join(sorted(skus))}.",
        )

    if duration is None:
        return _unavailable(
            EstimateUnavailable.MISSING_AXIS,
            f"{model.id} prices per second, but no duration was supplied.",
        )

    wanted_prefix = _MODE_PREFIXES[1] if has_frame_images else _MODE_PREFIXES[0]
    audio_keys = [
        key
        for key in duration_keys
        if (_AUDIO_MARKER in _base_key(key)) == generate_audio
    ]

    if not audio_keys:
        if generate_audio and model.generate_audio:
            return _unavailable(
                EstimateUnavailable.MISSING_AXIS,
                f"{model.id} generates audio but publishes no with-audio SKU, so "
                "the audio surcharge cannot be priced.",
            )
        return _unavailable(
            EstimateUnavailable.NO_MATCHING_SKU,
            f"{model.id} has no SKU matching generate_audio={generate_audio}.",
        )

    best = _best_match(
        audio_keys, skus, wanted_prefix=wanted_prefix, resolution=resolution
    )
    if best.key is None:
        return _unavailable(
            EstimateUnavailable.NO_MATCHING_SKU,
            f"{model.id} publishes no SKU for resolution={resolution} with "
            f"{'image' if has_frame_images else 'text'}-to-video.",
        )
    if best.ambiguous:
        return _unavailable(
            EstimateUnavailable.AMBIGUOUS_AXES,
            f"{model.id} has several equally specific SKUs at different prices; "
            "picking one would be a guess.",
        )

    if generate_audio:
        # If dropping the audio axis would have produced a strictly more
        # specific match, the axes are non-orthogonal and neither candidate is
        # correct — this is exactly the Kling case (spec 3.1 item 3).
        rival = _best_match(
            [key for key in duration_keys if _AUDIO_MARKER not in _base_key(key)],
            skus,
            wanted_prefix=wanted_prefix,
            resolution=resolution,
        )
        if rival.score > best.score:
            return _unavailable(
                EstimateUnavailable.AMBIGUOUS_AXES,
                f"{model.id} prices audio and resolution on separate, "
                "non-orthogonal SKU axes; no single SKU covers this request.",
            )

    amount = _quantise(parse_sku_amount(best.key, skus[best.key]) * duration)
    return Estimate(
        amount=amount,
        reason=None,
        detail=f"{duration}s at {skus[best.key]} per second ({best.key}).",
        sku_key=best.key,
    )


def reservation_amount(
    estimate: Estimate, *, max_job_cost_usd: Decimal
) -> tuple[Decimal, bool]:
    """The amount to reserve, and whether it came from an exact estimate.

    Exactly estimable -> (estimate.amount, True). Otherwise the pessimistic
    ceiling (spec 3.3): the cap must over-count rather than under-count, since
    under-counting lets it silently never trip.
    """
    if estimate.amount is not None:
        return estimate.amount, True
    return max_job_cost_usd, False
