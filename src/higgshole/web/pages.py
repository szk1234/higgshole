"""HTMX screens (spec section 6.1).

Every page is server-rendered and progressively enhanced with HTMX; there is
no build step and no bundler, and HTMX is vendored rather than loaded from a
CDN so the console works on an offline LAN.

Templates read the same view models the REST API returns, so the browser and
an agent can never see different data for the same generation.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from higgshole.store.db import (
    TERMINAL_STATES,
    GenerationKind,
    GenerationState,
    MediaFilter,
)
from higgshole.web.api import (
    _budget_out,
    _settings_out,
    current_key_status,
    error_response,
    generation_out,
    list_models,
    list_projects,
)
from higgshole.web.api import estimate as api_estimate
from higgshole.web.app import AppState, get_state

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"])


async def _chrome(request: Request, state: AppState, screen: str) -> dict:
    """The context every page shares: navigation plus the budget banner."""
    key_status = await current_key_status(state)
    return {
        "request": request,
        "screen": screen,
        "budget": _budget_out(await state.gate.status(key_status)),
    }


@router.get("/", response_class=HTMLResponse)
async def create_screen(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    models = await list_models(kind=None, state=state)
    context = await _chrome(request, state, "create")
    context |= {
        "models": [m for m in models if not m.is_favourite],
        "favourites": [m for m in models if m.is_favourite],
        "projects": await list_projects(state=state),
        "model": models[0] if models else None,
        "video_references_supported": _settings_out(state).video_references_supported,
        "estimate": None,
    }
    return templates.TemplateResponse(request, "create.html", context)


@router.get("/library", response_class=HTMLResponse)
async def library_screen(
    request: Request,
    project: str | None = Query(default=None),
    kind: GenerationKind | None = Query(default=None),
    model: str | None = Query(default=None),
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    filters = MediaFilter(project_slug=project, kind=kind, model=model, limit=60)
    context = await _chrome(request, state, "library")
    context |= {
        "projects": await list_projects(state=state),
        "selected_project": project,
        "items": [
            generation_out(state, row) for row in state.db.list_generations(filters)
        ],
    }
    return templates.TemplateResponse(request, "library.html", context)


@router.get("/library/{gen_id}", response_class=HTMLResponse)
async def detail_screen(
    request: Request, gen_id: str, state: AppState = Depends(get_state)
) -> HTMLResponse:
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")

    context = await _chrome(request, state, "library")
    context |= {"item": generation_out(state, row)}
    return templates.TemplateResponse(request, "detail.html", context)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_screen(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    in_flight = [s for s in GenerationState if s not in TERMINAL_STATES]
    rows = state.db.list_generations_in_states(in_flight)
    context = await _chrome(request, state, "jobs")
    context |= {"jobs": [generation_out(state, row) for row in rows]}
    return templates.TemplateResponse(request, "jobs.html", context)


@router.get("/settings", response_class=HTMLResponse)
async def settings_screen(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    context = await _chrome(request, state, "settings")
    context |= {
        "settings": _settings_out(state),
        "resume_report": state.resume_report,
    }
    return templates.TemplateResponse(request, "settings.html", context)


async def _capability(state: AppState, kind: GenerationKind, model_id: str | None):
    """Look up one model's capability record, or None when unselected."""
    if not model_id:
        return None
    models = await list_models(kind=kind, state=state)
    return next((m for m in models if m.id == model_id), None)


@router.get("/partials/model-controls", response_class=HTMLResponse)
async def model_controls_partial(
    request: Request,
    kind: GenerationKind = Query(default=GenerationKind.IMAGE),
    model: str | None = Query(default=None),
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Capability-derived controls for one model.

    Nothing here is hardcoded: every option comes from the cached catalogue,
    so an option the provider does not support is structurally impossible to
    offer (spec sections 2.7 and 6.1). Video reference slots are additionally
    gated on the transport being able to deliver a local file at all.
    """
    return templates.TemplateResponse(
        request,
        "partials/model_controls.html",
        {
            "model": await _capability(state, kind, model),
            "video_references_supported": _settings_out(state).video_references_supported,
        },
    )


@router.get("/partials/estimate", response_class=HTMLResponse)
async def estimate_partial(
    request: Request,
    kind: GenerationKind = Query(default=GenerationKind.IMAGE),
    model: str | None = Query(default=None),
    prompt: str = Query(default=""),
    duration: int | None = Query(default=None),
    resolution: str | None = Query(default=None),
    aspect_ratio: str | None = Query(default=None),
    quality: str | None = Query(default=None),
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Render the advisory estimate, or the reason there cannot be one.

    A missing estimate is shown as its machine-readable reason and its
    explanation — never as a placeholder number (spec section 3.2).
    """
    result = None
    if model:
        body = {
            "model": model,
            "prompt": prompt or " ",
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "quality": quality,
        }
        result = await api_estimate(
            kind=kind,
            body={k: v for k, v in body.items() if v is not None},
            state=state,
        )

    return templates.TemplateResponse(
        request, "partials/estimate.html", {"estimate": result}
    )


@router.get("/partials/library-grid", response_class=HTMLResponse)
async def library_grid_partial(
    request: Request,
    project: str | None = Query(default=None),
    kind: GenerationKind | None = Query(default=None),
    model: str | None = Query(default=None),
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    filters = MediaFilter(
        project_slug=project or None, kind=kind, model=model or None, limit=60
    )
    items = [generation_out(state, row) for row in state.db.list_generations(filters)]
    return templates.TemplateResponse(
        request, "partials/library_grid.html", {"items": items}
    )


@router.get("/partials/job-row", response_class=HTMLResponse)
async def job_row_partial(
    request: Request, gen_id: str = Query(...), state: AppState = Depends(get_state)
) -> HTMLResponse:
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")
    return templates.TemplateResponse(
        request, "partials/job_row.html", {"item": generation_out(state, row)}
    )
