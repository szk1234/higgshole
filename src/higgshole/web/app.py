"""Application factory, shared state, and lifespan.

Two deliberate structural decisions live here:

1. Media bytes are dispatched to a separate ASGI application BEFORE the
   FastAPI middleware stack runs. Mounting alone is not enough — Starlette
   middleware added with `add_middleware` wraps the whole router, mounts
   included — so the exemption is made explicit in `HiggsHoleApp.__call__`.
   That is what makes the spec section 6.3 guarantee hold against a future
   middleware addition rather than merely asking reviewers to remember it.

2. `GZipMiddleware` is never added. It compresses partial content while
   `Content-Range` still describes the uncompressed entity, and it does not
   exempt video/mp4.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import anyio
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from higgshole.budget.gate import BudgetGate
from higgshole.budget.ledger import Ledger
from higgshole.catalog.cache import CatalogCache
from higgshole.config import MediaKind, Settings, get_settings
from higgshole.jobs.resume import ResumeReport, resume_pending_jobs
from higgshole.jobs.runner import ImageJobRunner, VideoJobRunner
from higgshole.orclient.client import OpenRouterClient
from higgshole.store.db import Database
from higgshole.store.paths import MediaPaths
from higgshole.web import sse
from higgshole.web.media import (
    MEDIA_MOUNT_PATH,
    THUMBS_MOUNT_PATH,
    create_media_app,
)
from higgshole.web.sse import EventBus

#: Path prefixes dispatched ahead of the middleware stack.
MEDIA_PREFIXES: tuple[str, ...] = (f"{MEDIA_MOUNT_PATH}/", f"{THUMBS_MOUNT_PATH}/")


@dataclass
class AppState:
    """Everything the request handlers need, stored on `app.state.higgshole`.

    Mutable because `resume_report` and the key-status cache are filled in
    during and after startup.
    """

    settings: Settings
    db: Database
    paths: MediaPaths
    catalog: CatalogCache
    ledger: Ledger
    gate: BudgetGate
    image_runner: ImageJobRunner
    video_runner: VideoJobRunner
    events: EventBus
    resume_report: ResumeReport | None = None
    client_factory: Callable[[MediaKind], OpenRouterClient] | None = None
    key_status_cached: tuple[float, object] | None = None


def resolve_api_key(db: Database, settings: Settings, kind: MediaKind) -> str | None:
    """The settings table overlays the environment.

    Spec section 8 says keys may alternatively be set through the UI, so a
    value saved in the database must win over the environment; within each
    source the per-kind key wins over the shared one. Resolved on every call
    rather than once at startup so a key saved in the UI takes effect without
    a restart.
    """
    stored_specific = db.get_setting(f"openrouter_api_key_{kind}")
    if stored_specific:
        return stored_specific
    stored_shared = db.get_setting("openrouter_api_key")
    if stored_shared:
        return stored_shared
    return settings.openrouter_api_key_for(kind)


def resolve_daily_cap(db: Database, settings: Settings) -> Decimal | None:
    """The cap saved through the UI overlays the environment cap.

    Same rule as `resolve_api_key`: a cap entered in Settings that only the
    settings screen could see would be a cap that never guards anything.
    """
    stored = db.get_setting("daily_cap_usd")
    if stored:
        try:
            return Decimal(stored)
        except InvalidOperation:
            # A malformed row must not silently disable the environment cap.
            return settings.daily_cap_usd
    return settings.daily_cap_usd


def build_app_state(settings: Settings, db: Database | None = None) -> AppState:
    """Assemble the real object graph. Tests substitute their own AppState.

    The database must already be migrated: `resolve_daily_cap` reads the
    settings table while the gate is being constructed, so assembling against
    an empty file would fail on a fresh installation.
    """
    database = db if db is not None else Database.from_settings(settings)
    paths = MediaPaths.from_settings(settings)
    events = EventBus()

    def client_factory(kind: MediaKind) -> OpenRouterClient:
        """A fresh client per call, with the key resolved at call time, so a
        key saved through the UI takes effect on the next request rather than
        at the next restart."""
        key = resolve_api_key(database, settings, kind)
        # OpenRouterClient raises AuthError on a blank key, which callers
        # already treat as "provider unavailable" rather than crashing.
        return OpenRouterClient(key or "")

    # Not `CatalogCache.from_settings`: that builds its own environment-only
    # factory, so a key saved through the UI would never reach a catalogue
    # refresh or a lazy image-pricing fetch.
    catalog = CatalogCache(database, client_factory, ttl_hours=settings.catalog_ttl_hours)
    ledger = Ledger(database)
    # Not `BudgetGate.from_settings`: that reads the environment only, so a
    # cap saved through the UI would never be enforced.
    gate = BudgetGate(
        database,
        ledger,
        daily_cap_usd=resolve_daily_cap(database, settings),
        max_job_cost_usd=settings.max_job_cost_usd,
        max_in_flight=settings.max_in_flight,
    )

    common = {
        "db": database,
        "paths": paths,
        "gate": gate,
        "catalog": catalog,
        "settings": settings,
        "client_factory": client_factory,
        "events": events,
    }

    return AppState(
        settings=settings,
        db=database,
        paths=paths,
        catalog=catalog,
        ledger=ledger,
        gate=gate,
        image_runner=ImageJobRunner(**common),
        video_runner=VideoJobRunner(**common),
        events=events,
        client_factory=client_factory,
    )


def get_state(request: Request) -> AppState:
    """FastAPI dependency returning the assembled application state."""
    return request.app.state.higgshole


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Migrate, refresh, resume, then serve; unwind in reverse on shutdown."""
    state: AppState | None = getattr(app.state, "higgshole", None)
    if state is None:
        # Migrate before assembling: the gate resolves the daily cap out of
        # the settings table, which does not exist yet on a fresh install.
        await anyio.to_thread.run_sync(app.state.db_override.migrate)
        state = build_app_state(app.state.settings, app.state.db_override)
        app.state.higgshole = state

    await anyio.to_thread.run_sync(state.db.migrate)
    await anyio.to_thread.run_sync(state.db.ensure_default_project)

    # Spec section 4.2: a catalogue failure is surfaced, never fatal.
    try:
        await state.catalog.refresh_if_stale()
    except Exception:  # noqa: BLE001 - startup must not depend on the provider
        pass

    state.resume_report = await resume_pending_jobs(
        db=state.db,
        runner=state.video_runner,
        ledger=state.ledger,
        settings=state.settings,
    )

    stop = anyio.Event()
    refresher = asyncio.create_task(state.catalog.run_periodic_refresh(stop=stop))
    app.state.catalog_refresher = refresher

    try:
        yield
    finally:
        stop.set()
        refresher.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await refresher
        await state.video_runner.shutdown()
        await anyio.to_thread.run_sync(state.db.close)


class HiggsHoleApp(FastAPI):
    """A FastAPI application that serves media outside its middleware stack.

    `Starlette.__call__` delegates to `self.middleware_stack`, so anything
    routed normally is wrapped by every registered middleware. Intercepting
    the media prefixes here — above that call — is what makes the exemption
    structural rather than a convention someone can accidentally undo.
    """

    media_app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.media_app is not None:
            path = scope.get("path", "")
            if path.startswith(MEDIA_PREFIXES):
                await self.media_app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


def create_app(
    *, settings: Settings | None = None, db: Database | None = None
) -> HiggsHoleApp:
    """Build the application.

    No middleware is registered. If any is ever added, it applies to the API
    and pages only — media is dispatched before the stack exists.
    """
    resolved = settings or get_settings()

    # Exactly one Database for the process. Plan 2's Database wraps a single
    # SQLite connection under a single worker, and opening a second one for
    # the media sub-application would leave it unclosed at shutdown.
    database = db if db is not None else Database.from_settings(resolved)

    app = HiggsHoleApp(
        title="HiggsHole",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.state.db_override = database

    @app.exception_handler(HTTPException)
    async def _flat_error(request: Request, exc: HTTPException) -> JSONResponse:
        """Serialise structured errors at the top level rather than nested
        under "detail", so that both the browser and the MCP client read one
        shape: `body["error"]`, `body["message"]`, `body["issues"]`."""
        if isinstance(exc.detail, dict):
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
                headers=getattr(exc, "headers", None),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "message": str(exc.detail)},
            headers=getattr(exc, "headers", None),
        )

    paths = MediaPaths.from_settings(resolved)
    app.media_app = create_media_app(paths, database)

    from fastapi.staticfiles import StaticFiles

    from higgshole.web import api, pages  # here, to avoid a circular import

    app.include_router(api.router)
    app.include_router(sse.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=str(pages.STATIC_DIR)), name="static")
    return app


def run() -> None:
    """Console entrypoint.

    Exactly one worker: video pollers are in-process asyncio tasks and the
    reservation lock is process-local, so a second worker would reattach a
    poller to the same job and double-count its spend (spec section 9).
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "higgshole.web.app:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        workers=1,
        log_level="info",
    )
