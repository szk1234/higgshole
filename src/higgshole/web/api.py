"""The REST surface.

This is the only interface `mcp_server.py` depends on, so a path or a field
name here is a public contract: changing one is a breaking change for the MCP
layer (spec section 4.1). Every monetary value leaves as a string or null —
never a JSON number, and never 0 to mean unknown (spec section 3.4).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from higgshole.budget.ledger import BudgetStatus
from higgshole.catalog.validation import Severity, ValidationIssue
from higgshole.orclient.errors import (
    AuthError,
    IndeterminateError,
    InsufficientCreditsError,
    InvalidRequestError,
    ModerationError,
    OpenRouterError,
    ProviderError,
    RateLimitError,
)
from higgshole.orclient.types import ImageModel, KeyStatus, VideoModel
from higgshole.store.db import (
    AssetKind,
    AssetRow,
    DuplicateSlugError,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
)
from higgshole.web.app import AppState, get_state
from higgshole.web.media import media_url_for, poster_url_for, thumb_url_for

router = APIRouter(prefix="/api", tags=["api"])

#: The free GET /api/v1/key call is cached for a minute (spec section 3.2):
#: budget is shown on every page, and a per-request round trip would make the
#: UI feel slower without making the figure meaningfully fresher.
KEY_STATUS_TTL_SECONDS: float = 60.0

FAVOURITES_SETTING = "favourite_models"


# -- response models -------------------------------------------------------


class ValidationIssueOut(BaseModel):
    parameter: str
    value: str
    severity: Severity
    message: str


class ErrorOut(BaseModel):
    error: str
    message: str
    issues: list[ValidationIssueOut] = []


class ModelCapability(BaseModel):
    id: str
    kind: GenerationKind
    name: str
    supported_resolutions: list[str] = []
    supported_aspect_ratios: list[str] = []
    supported_durations: list[int] = []
    supported_sizes: list[str] = []
    supported_frame_images: list[str] = []
    max_input_references: int = 0
    quality_values: list[str] = []
    generate_audio: bool | None = None
    seed: bool = False
    is_favourite: bool = False


class EstimateOut(BaseModel):
    amount_usd: str | None
    estimate_unavailable: str | None
    detail: str


class AssetOut(BaseModel):
    id: str
    kind: AssetKind
    mime_type: str
    bytes: int
    width: int | None
    height: int | None
    duration_s: float | None
    local_path: str
    url: str
    created_at: str


class LineageOut(BaseModel):
    asset_id: str
    role: InputRole
    position: int
    generation_id: str | None
    thumb_url: str | None


class GenerationOut(BaseModel):
    id: str
    kind: GenerationKind
    project_slug: str
    model: str
    prompt: str
    params: dict[str, Any]
    state: GenerationState
    provider_job_id: str | None
    error_reason: ErrorReason | None
    error_detail: str | None
    cost_usd: str | None
    cost_known: bool
    asset: AssetOut | None
    thumb_url: str | None
    poster_url: str | None
    inputs: list[LineageOut]
    created_at: str
    updated_at: str
    completed_at: str | None


class ProjectOut(BaseModel):
    id: str
    slug: str
    name: str
    created_at: str
    item_count: int


class BudgetOut(BaseModel):
    provider_limit_usd: str | None
    provider_remaining_usd: str | None
    provider_usage_daily_usd: str | None
    provider_available: bool
    cap_usd: str | None
    spent_today_usd: str
    remaining_today_usd: str | None
    is_lower_bound: bool
    in_flight: int
    max_in_flight: int


class MediaListOut(BaseModel):
    items: list[GenerationOut]
    total: int
    limit: int
    offset: int


class CatalogStatusOut(BaseModel):
    image_fetched_at: str | None
    video_fetched_at: str | None
    is_stale: bool
    last_error: str | None


class CreateProjectIn(BaseModel):
    name: str


# -- helpers ---------------------------------------------------------------


def mask_key(value: str | None) -> str | None:
    """Reduce a key to an unambiguous but useless suffix.

    The only function permitted to put key material into a response, and it
    never reveals more than the final four characters (spec section 7).
    """
    if not value:
        return None
    return f"...{value[-4:]}"


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    issues: Sequence[ValidationIssue] = (),
) -> HTTPException:
    """Build an HTTPException carrying the uniform ErrorOut body.

    The body is put in `detail` because that is the only slot HTTPException
    has; the handler registered in `create_app` unwraps it so the response
    is `{"error", "message", "issues"}` at the top level rather than nested.
    """
    body = ErrorOut(
        error=code,
        message=message,
        issues=[
            ValidationIssueOut(
                parameter=issue.parameter,
                value=issue.value,
                severity=issue.severity,
                message=issue.message,
            )
            for issue in issues
        ],
    )
    return HTTPException(status_code=status_code, detail=body.model_dump(mode="json"))


#: Provider error -> (HTTP status, stable code). Spec section 10.
_PROVIDER_ERROR_MAP: dict[type[OpenRouterError], tuple[int, str]] = {
    ModerationError: (422, "moderation_refused"),
    InvalidRequestError: (400, "validation_failed"),
    AuthError: (401, "validation_failed"),
    InsufficientCreditsError: (402, "provider_credit_limit"),
    RateLimitError: (429, "in_flight_limit"),
    IndeterminateError: (502, "indeterminate"),
    ProviderError: (502, "provider_unavailable"),
}


def map_openrouter_error(exc: OpenRouterError) -> HTTPException:
    """Translate a provider error into the API's stable vocabulary."""
    for error_type, (status, code) in _PROVIDER_ERROR_MAP.items():
        if isinstance(exc, error_type):
            return error_response(status, code, exc.message)
    return error_response(500, "internal_error", exc.message)


def _decimal_out(value: Decimal | None) -> str | None:
    """Decimal as a string, preserving None. Never a float, never 0 for
    unknown (spec section 3.4)."""
    return None if value is None else str(value)


def _favourites(state: AppState) -> set[str]:
    raw = state.db.get_setting(FAVOURITES_SETTING)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set()


def _video_capability(model: VideoModel, favourites: set[str]) -> ModelCapability:
    return ModelCapability(
        id=model.id,
        kind=GenerationKind.VIDEO,
        name=model.id,
        supported_resolutions=list(model.supported_resolutions),
        supported_aspect_ratios=list(model.supported_aspect_ratios),
        supported_durations=list(model.supported_durations),
        supported_sizes=list(model.supported_sizes),
        supported_frame_images=list(model.supported_frame_images),
        generate_audio=model.generate_audio,
        seed=model.seed,
        is_favourite=model.id in favourites,
    )


def _image_capability(model: ImageModel, favourites: set[str]) -> ModelCapability:
    return ModelCapability(
        id=model.id,
        kind=GenerationKind.IMAGE,
        name=model.name or model.id,
        max_input_references=model.max_input_references,
        quality_values=list(model.quality_values),
        seed=False,
        is_favourite=model.id in favourites,
    )


def _asset_out(state: AppState, asset: AssetRow) -> AssetOut:
    """Every asset carries BOTH a host path and a URL, since agents run on
    the same host as the service (spec section 6.2)."""
    return AssetOut(
        id=asset.id,
        kind=asset.kind,
        mime_type=asset.mime_type,
        bytes=asset.bytes,
        width=asset.width,
        height=asset.height,
        duration_s=asset.duration_s,
        local_path=str(state.paths.root / asset.file_path),
        url=media_url_for(asset.file_path),
        created_at=asset.created_at,
    )


def generation_out(state: AppState, row: GenerationRow) -> GenerationOut:
    """Assemble the full public view of one generation."""
    project = state.db.get_project(row.project_id)
    slug = project.slug if project is not None else "unsorted"

    output = next(
        (
            asset
            for asset in state.db.list_assets_for_generation(row.id)
            if asset.kind is AssetKind.OUTPUT
        ),
        None,
    )

    amount, cost_known = state.ledger.generation_total(row.id)

    inputs: list[LineageOut] = []
    for link in state.db.list_generation_inputs(row.id):
        asset = state.db.get_asset(link.asset_id)
        inputs.append(
            LineageOut(
                asset_id=link.asset_id,
                role=link.role,
                position=link.position,
                generation_id=asset.generation_id if asset is not None else None,
                thumb_url=(
                    thumb_url_for(project_slug=slug, gen_id=asset.generation_id)
                    if asset is not None and asset.generation_id
                    else None
                ),
            )
        )

    complete = row.state is GenerationState.COMPLETE
    return GenerationOut(
        id=row.id,
        kind=row.kind,
        project_slug=slug,
        model=row.model,
        prompt=row.prompt,
        params=row.params,
        state=row.state,
        provider_job_id=row.provider_job_id,
        error_reason=row.error_reason,
        error_detail=row.error_detail,
        cost_usd=_decimal_out(amount if cost_known else None),
        cost_known=cost_known,
        asset=_asset_out(state, output) if output is not None else None,
        thumb_url=thumb_url_for(project_slug=slug, gen_id=row.id) if complete else None,
        poster_url=(
            poster_url_for(project_slug=slug, gen_id=row.id)
            if complete and row.kind is GenerationKind.VIDEO
            else None
        ),
        inputs=inputs,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


async def current_key_status(state: AppState) -> KeyStatus | None:
    """The provider's authoritative figures, cached for KEY_STATUS_TTL_SECONDS.

    Returns None when the call fails, which the UI renders as "local only"
    rather than silently presenting ledger figures as authoritative.
    """
    now = time.monotonic()
    cached = state.key_status_cached
    if cached is not None and now - cached[0] < KEY_STATUS_TTL_SECONDS:
        return cached[1]  # type: ignore[return-value]

    if state.client_factory is None:
        return None

    try:
        # `async with`, as the runners do: the factory opens a fresh
        # httpx.AsyncClient per call and this runs on every page render and
        # on every /api/budget past the TTL, so a leak here is unbounded.
        async with state.client_factory("image") as client:
            status = await client.get_key_status()
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return None

    state.key_status_cached = (now, status)
    return status


def _budget_out(status: BudgetStatus) -> BudgetOut:
    return BudgetOut(
        provider_limit_usd=_decimal_out(status.provider_limit),
        provider_remaining_usd=_decimal_out(status.provider_remaining),
        provider_usage_daily_usd=_decimal_out(status.provider_usage_daily),
        provider_available=status.provider_available,
        cap_usd=_decimal_out(status.cap),
        spent_today_usd=str(status.spent_today),
        remaining_today_usd=_decimal_out(status.remaining_today),
        is_lower_bound=status.is_lower_bound,
        in_flight=status.in_flight,
        max_in_flight=status.max_in_flight,
    )


# -- routes ----------------------------------------------------------------


@router.get("/models", response_model=list[ModelCapability])
async def list_models(
    kind: GenerationKind | None = Query(default=None),
    state: AppState = Depends(get_state),
) -> list[ModelCapability]:
    """Every model the catalogue knows, with its discovered constraints.

    The UI renders controls from this and nothing else, so an option a model
    does not declare is never offered (spec section 6.1).
    """
    favourites = _favourites(state)
    entries: list[ModelCapability] = []

    if kind in (None, GenerationKind.VIDEO):
        entries += [
            _video_capability(m, favourites)
            for m in await state.catalog.get_video_models()
        ]
    if kind in (None, GenerationKind.IMAGE):
        entries += [
            _image_capability(m, favourites)
            for m in await state.catalog.get_image_models()
        ]

    return entries


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(state: AppState = Depends(get_state)) -> list[ProjectOut]:
    from higgshole.store.db import MediaFilter

    result: list[ProjectOut] = []
    for project in state.db.list_projects():
        count = state.db.count_generations(MediaFilter(project_slug=project.slug))
        result.append(
            ProjectOut(
                id=project.id,
                slug=project.slug,
                name=project.name,
                created_at=project.created_at,
                item_count=count,
            )
        )
    return result


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    body: CreateProjectIn, state: AppState = Depends(get_state)
) -> ProjectOut:
    try:
        project = state.db.create_project(name=body.name)
    except DuplicateSlugError as exc:
        raise error_response(409, "validation_failed", str(exc)) from exc

    return ProjectOut(
        id=project.id,
        slug=project.slug,
        name=project.name,
        created_at=project.created_at,
        item_count=0,
    )


@router.get("/budget", response_model=BudgetOut)
async def get_budget(state: AppState = Depends(get_state)) -> BudgetOut:
    """Provider-authoritative credit plus local cap status (spec section 3.2)."""
    key_status = await current_key_status(state)
    return _budget_out(await state.gate.status(key_status))
