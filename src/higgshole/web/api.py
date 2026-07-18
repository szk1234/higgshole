"""The REST surface.

This is the only interface `mcp_server.py` depends on, so a path or a field
name here is a public contract: changing one is a breaking change for the MCP
layer (spec section 4.1). Every monetary value leaves as a string or null —
never a JSON number, and never 0 to mean unknown (spec section 3.4).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from higgshole.budget.estimator import (
    Estimate,
    estimate_image_cost,
    estimate_video_cost,
)
from higgshole.budget.ledger import BudgetStatus
from higgshole.catalog.validation import Severity, ValidationIssue, has_hard_failure
from higgshole.jobs.runner import GenerationOutcome, GenerationRequest
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
    TERMINAL_STATES,
    AssetKind,
    AssetRow,
    DuplicateSlugError,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    MediaFilter,
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


#: How often a long-poll re-reads the row. Short enough to feel immediate,
#: long enough that a five-minute wait is 600 cheap reads, not 300,000.
JOB_POLL_INTERVAL_S: float = 0.5

#: Non-terminal states, derived so a new state cannot be forgotten here.
_IN_FLIGHT_STATES = tuple(s for s in GenerationState if s not in TERMINAL_STATES)


class GenerateImageIn(BaseModel):
    model: str
    prompt: str
    project: str = "unsorted"
    aspect_ratio: str | None = None
    resolution: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None
    background: str | None = None
    seed: int | None = None
    input_reference_asset_ids: list[str] = []


class GenerateVideoIn(BaseModel):
    model: str
    prompt: str
    project: str = "unsorted"
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    generate_audio: bool | None = None
    seed: int | None = None
    first_frame_asset_id: str | None = None
    last_frame_asset_id: str | None = None
    input_reference_asset_ids: list[str] = []


#: Rejection/failure reason -> (HTTP status, stable code). Anything absent is
#: reported as a completed-but-failed generation rather than an HTTP error.
_REASON_STATUS: dict[ErrorReason, tuple[int, str]] = {
    ErrorReason.VALIDATION: (422, "validation_failed"),
    ErrorReason.CAP_EXCEEDED: (402, "local_daily_cap"),
    ErrorReason.IN_FLIGHT_LIMIT: (429, "in_flight_limit"),
    ErrorReason.INSUFFICIENT_CREDITS: (402, "provider_credit_limit"),
    ErrorReason.MODERATION: (422, "moderation_refused"),
    ErrorReason.INDETERMINATE: (502, "indeterminate"),
}


def _image_params(body: GenerateImageIn) -> dict[str, Any]:
    fields = (
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "background",
        "seed",
    )
    return {f: getattr(body, f) for f in fields if getattr(body, f) is not None}


def _video_params(body: GenerateVideoIn) -> dict[str, Any]:
    fields = (
        "duration",
        "resolution",
        "aspect_ratio",
        "size",
        "generate_audio",
        "seed",
    )
    return {f: getattr(body, f) for f in fields if getattr(body, f) is not None}


def _require_project(state: AppState, slug: str):
    project = state.db.get_project_by_slug(slug)
    if project is None:
        raise error_response(404, "project_not_found", f"No project with slug {slug!r}.")
    return project


async def _require_image_model(state: AppState, model_id: str) -> ImageModel:
    model = await state.catalog.get_image_model(model_id)
    if model is None:
        raise error_response(404, "model_not_found", f"Unknown image model {model_id!r}.")
    return model


async def _require_video_model(state: AppState, model_id: str) -> VideoModel:
    model = await state.catalog.get_video_model(model_id)
    if model is None:
        raise error_response(404, "model_not_found", f"Unknown video model {model_id!r}.")
    return model


def _outcome_or_raise(state: AppState, outcome: GenerationOutcome) -> GenerationOut:
    """Turn a runner outcome into a response, or into the matching error.

    A budget rejection is a normal result inside the runner, but it is an
    error at the HTTP boundary: an agent must be able to branch on the code
    without parsing prose (spec section 10).
    """
    if outcome.state in (GenerationState.REJECTED, GenerationState.FAILED):
        mapped = (
            _REASON_STATUS.get(outcome.error_reason) if outcome.error_reason else None
        )
        if mapped is not None:
            status, code = mapped
            raise error_response(status, code, outcome.error_detail or outcome.state.value)

    row = state.db.get_generation(outcome.generation_id)
    if row is None:  # pragma: no cover - the runner just wrote it
        raise error_response(500, "internal_error", "generation row vanished")
    return generation_out(state, row)


@router.post("/estimate", response_model=EstimateOut)
async def estimate(
    kind: GenerationKind = Query(...),
    body: dict[str, Any] = Body(...),
    state: AppState = Depends(get_state),
) -> EstimateOut:
    """An advisory pre-flight cost, or an explicit reason it cannot be given."""
    if kind is GenerationKind.IMAGE:
        request = GenerateImageIn.model_validate(body)
        model = await _require_image_model(state, request.model)
        pricing = await state.catalog.get_image_pricing(model.id)
        result: Estimate = estimate_image_cost(
            model,
            pricing,
            quality=request.quality,
            reference_count=len(request.input_reference_asset_ids),
        )
    else:
        request_v = GenerateVideoIn.model_validate(body)
        video_model = await _require_video_model(state, request_v.model)
        result = estimate_video_cost(
            video_model,
            duration=request_v.duration,
            resolution=request_v.resolution,
            aspect_ratio=request_v.aspect_ratio,
            generate_audio=bool(request_v.generate_audio),
            has_frame_images=bool(
                request_v.first_frame_asset_id or request_v.last_frame_asset_id
            ),
        )

    return EstimateOut(
        amount_usd=_decimal_out(result.amount),
        estimate_unavailable=result.reason.value if result.reason else None,
        detail=result.detail,
    )


@router.post("/generate/image", response_model=GenerationOut)
async def generate_image(
    body: GenerateImageIn, state: AppState = Depends(get_state)
) -> GenerationOut:
    """Synchronous: returns the finished asset (spec section 6.2)."""
    await _require_image_model(state, body.model)
    project = _require_project(state, body.project)

    request = GenerationRequest(
        kind=GenerationKind.IMAGE,
        project_id=project.id,
        project_slug=project.slug,
        model=body.model,
        prompt=body.prompt,
        params=_image_params(body),
        inputs=tuple(
            (asset_id, InputRole.INPUT_REFERENCE)
            for asset_id in body.input_reference_asset_ids
        ),
    )

    # Ordering is fixed: local validation, budget gate, dispatch. Validating
    # here as well as in the runner costs nothing and is what lets the API
    # return structured issues instead of a bare message.
    issues = await state.image_runner.validate(request)
    if has_hard_failure(issues):
        raise error_response(
            422,
            "validation_failed",
            "The request violates the model's constraints.",
            issues=issues,
        )

    try:
        outcome = await state.image_runner.run(request)
    except OpenRouterError as exc:
        raise map_openrouter_error(exc) from exc

    return _outcome_or_raise(state, outcome)


@router.post("/generate/video", response_model=GenerationOut, status_code=202)
async def generate_video(
    body: GenerateVideoIn, state: AppState = Depends(get_state)
) -> GenerationOut:
    """Returns as soon as the provider job ID is committed; never blocks."""
    await _require_video_model(state, body.model)
    project = _require_project(state, body.project)

    inputs: list[tuple[str, InputRole]] = []
    if body.first_frame_asset_id:
        inputs.append((body.first_frame_asset_id, InputRole.FIRST_FRAME))
    if body.last_frame_asset_id:
        inputs.append((body.last_frame_asset_id, InputRole.LAST_FRAME))
    inputs += [
        (asset_id, InputRole.INPUT_REFERENCE)
        for asset_id in body.input_reference_asset_ids
    ]

    request = GenerationRequest(
        kind=GenerationKind.VIDEO,
        project_id=project.id,
        project_slug=project.slug,
        model=body.model,
        prompt=body.prompt,
        params=_video_params(body),
        inputs=tuple(inputs),
    )

    issues = await state.video_runner.validate(request)
    if has_hard_failure(issues):
        raise error_response(
            422,
            "validation_failed",
            "The request violates the model's constraints.",
            issues=issues,
        )

    try:
        outcome = await state.video_runner.submit(request)
    except OpenRouterError as exc:
        raise map_openrouter_error(exc) from exc

    return _outcome_or_raise(state, outcome)


@router.get("/jobs", response_model=list[GenerationOut])
async def list_jobs(state: AppState = Depends(get_state)) -> list[GenerationOut]:
    """Every generation not yet in a terminal state."""
    rows = state.db.list_generations_in_states(_IN_FLIGHT_STATES)
    return [generation_out(state, row) for row in rows]


@router.get("/jobs/{gen_id}", response_model=GenerationOut)
async def get_job(
    gen_id: str,
    wait_seconds: int = Query(default=0, ge=0, le=600),
    state: AppState = Depends(get_state),
) -> GenerationOut:
    """Current status, optionally long-polled.

    The wait is bounded by the caller, not by the server, so an agent's own
    timeout governs how long it blocks (spec section 6.2).
    """
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")

    deadline = asyncio.get_running_loop().time() + wait_seconds
    while row.state not in TERMINAL_STATES:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(JOB_POLL_INTERVAL_S)
        refreshed = state.db.get_generation(gen_id)
        if refreshed is None:  # pragma: no cover - deleted mid-poll
            break
        row = refreshed

    return generation_out(state, row)
