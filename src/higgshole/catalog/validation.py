"""Request validation against discovered model capabilities.

Validation runs locally before dispatch so that an invalid combination costs
nothing rather than becoming a failed paid request.

The declared capability lists are not fully trustworthy: on four of sixteen
video models they contradict the model's own pricing (spec section 2.7). The
precedence rule below resolves that — a value the catalogue omits but the
pricing table covers is probably usable, so it warns rather than blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from higgshole.orclient.types import ImageModel, VideoModel


class Severity(StrEnum):
    HARD = "hard"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class ValidationIssue:
    parameter: str
    value: str
    severity: Severity
    message: str


def has_hard_failure(issues: Iterable[ValidationIssue]) -> bool:
    return any(issue.severity is Severity.HARD for issue in issues)


def _priced_for(model: VideoModel, value: str) -> bool:
    """Whether any pricing SKU key mentions this value.

    SKU keys embed the resolution, e.g. "image_to_video_duration_seconds_1080p".
    """
    needle = value.lower()
    return any(needle in key.lower() for key in model.pricing_skus)


def _check_video_resolution(model: VideoModel, resolution: str) -> ValidationIssue | None:
    if resolution in model.supported_resolutions:
        return None

    if _priced_for(model, resolution):
        return ValidationIssue(
            parameter="resolution",
            value=resolution,
            severity=Severity.ADVISORY,
            message=(
                f"{model.id} does not declare {resolution}, but prices it. "
                "It will probably work; the request will be sent."
            ),
        )

    declared = ", ".join(model.supported_resolutions) or "none"
    return ValidationIssue(
        parameter="resolution",
        value=resolution,
        severity=Severity.HARD,
        message=f"{model.id} does not support {resolution}. Supported: {declared}.",
    )


def validate_video_request(
    model: VideoModel,
    *,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    duration: int | None = None,
    frame_types: Sequence[str] = (),
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if resolution is not None:
        issue = _check_video_resolution(model, resolution)
        if issue:
            issues.append(issue)

    if aspect_ratio is not None and model.supported_aspect_ratios:
        if aspect_ratio not in model.supported_aspect_ratios:
            issues.append(
                ValidationIssue(
                    parameter="aspect_ratio",
                    value=aspect_ratio,
                    severity=Severity.HARD,
                    message=(
                        f"{model.id} does not support {aspect_ratio}. Supported: "
                        f"{', '.join(model.supported_aspect_ratios)}."
                    ),
                )
            )

    if duration is not None and model.supported_durations:
        if duration not in model.supported_durations:
            supported = ", ".join(str(d) for d in model.supported_durations)
            issues.append(
                ValidationIssue(
                    parameter="duration",
                    value=str(duration),
                    severity=Severity.HARD,
                    message=(
                        f"{model.id} does not support a {duration}s duration. "
                        f"Supported: {supported}."
                    ),
                )
            )

    for frame_type in frame_types:
        if not model.accepts_frame_images:
            issues.append(
                ValidationIssue(
                    parameter="frame_images",
                    value=frame_type,
                    severity=Severity.HARD,
                    message=(
                        f"{model.id} accepts no reference images; it is "
                        "text-to-video only."
                    ),
                )
            )
            break
        if frame_type not in model.supported_frame_images:
            accepted = ", ".join(model.supported_frame_images)
            issues.append(
                ValidationIssue(
                    parameter="frame_images",
                    value=frame_type,
                    severity=Severity.HARD,
                    message=f"{model.id} accepts only: {accepted}.",
                )
            )

    return issues


def validate_image_request(
    model: ImageModel,
    *,
    n: int = 1,
    quality: str | None = None,
    reference_count: int = 0,
    daily_cap_set: bool = False,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if n != 1:
        issues.append(
            ValidationIssue(
                parameter="n",
                value=str(n),
                severity=Severity.HARD,
                message=(
                    "Batch generation is not supported; n is fixed at 1 so that "
                    "each generation has its own cost record."
                ),
            )
        )

    if reference_count > model.max_input_references:
        issues.append(
            ValidationIssue(
                parameter="input_references",
                value=str(reference_count),
                severity=Severity.HARD,
                message=(
                    f"{model.id} accepts at most {model.max_input_references} "
                    f"reference image(s); {reference_count} were supplied."
                ),
            )
        )

    if quality is not None:
        if model.quality_values and quality not in model.quality_values:
            issues.append(
                ValidationIssue(
                    parameter="quality",
                    value=quality,
                    severity=Severity.HARD,
                    message=(
                        f"{model.id} does not support quality={quality}. "
                        f"Supported: {', '.join(model.quality_values)}."
                    ),
                )
            )
        elif quality == "auto" and daily_cap_set:
            issues.append(
                ValidationIssue(
                    parameter="quality",
                    value=quality,
                    severity=Severity.HARD,
                    message=(
                        "quality=auto has no cost ceiling and is refused while a "
                        "daily spend cap is configured. Choose an explicit quality."
                    ),
                )
            )

    return issues
