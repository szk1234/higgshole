"""Test doubles for the web layer.

Real `Database`, `MediaPaths`, `Ledger` and `BudgetGate` objects are used
throughout: they are cheap, offline, and exercising them catches wiring bugs
the fakes would hide. Only the catalogue and the job runners are faked, since
those are the two components that would otherwise reach the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from higgshole.budget.gate import BudgetGate
from higgshole.budget.ledger import Ledger
from higgshole.catalog.cache import CatalogStatus
from higgshole.config import Settings
from higgshole.jobs.runner import GenerationOutcome, GenerationRequest
from higgshole.orclient.types import ImageModel, KeyStatus, VideoModel
from higgshole.store.db import Database, ErrorReason, GenerationKind, GenerationState
from higgshole.store.paths import MediaPaths
from higgshole.web.app import AppState
from higgshole.web.sse import EventBus

KLING = VideoModel.from_api(
    {
        "id": "kwaivgi/kling-v3.0-pro",
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_durations": [5, 10],
        "supported_sizes": ["1280x720"],
        "supported_frame_images": ["first_frame", "last_frame"],
        "generate_audio": True,
        "seed": True,
        "pricing_skus": {"duration_seconds": "0.112"},
    }
)

SORA = VideoModel.from_api(
    {
        "id": "openai/sora-2-pro",
        "supported_resolutions": ["720p", "1080p"],
        "supported_durations": [4, 8],
        "supported_frame_images": [],
    }
)

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
        "name": "GPT Image 2",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]},
            "n": {"type": "range", "min": 1, "max": 10},
            "input_references": {"type": "range", "min": 0, "max": 16},
        },
    }
)

RECRAFT = ImageModel.from_api(
    {
        "id": "recraft/recraft-v4.1",
        "name": "Recraft v4.1",
        "supported_parameters": {
            "input_references": {"type": "range", "min": 0, "max": 1}
        },
    }
)


class FakeCatalog:
    """A CatalogCache stand-in that never opens a socket."""

    def __init__(
        self,
        *,
        video_models: tuple[VideoModel, ...] = (KLING, SORA),
        image_models: tuple[ImageModel, ...] = (GPT_IMAGE, RECRAFT),
        pricing: list[dict[str, Any]] | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        self._video = video_models
        self._image = image_models
        self._pricing = (
            pricing
            if pricing is not None
            else [{"billable": "output_image", "unit": "image", "cost_usd": 0.04}]
        )
        self._refresh_error = refresh_error
        self.refresh_calls = 0
        self.periodic_started = False

    async def get_video_models(self) -> tuple[VideoModel, ...]:
        return self._video

    async def get_image_models(self) -> tuple[ImageModel, ...]:
        return self._image

    async def get_video_model(self, model_id: str) -> VideoModel | None:
        return next((m for m in self._video if m.id == model_id), None)

    async def get_image_model(self, model_id: str) -> ImageModel | None:
        return next((m for m in self._image if m.id == model_id), None)

    async def get_image_pricing(self, model_id: str) -> list[dict[str, Any]]:
        return self._pricing

    async def refresh(self, *, force: bool = False) -> CatalogStatus:
        self.refresh_calls += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        return self.status()

    async def refresh_if_stale(self) -> CatalogStatus:
        return await self.refresh()

    def status(self) -> CatalogStatus:
        return CatalogStatus(
            image_fetched_at="2026-07-18T00:00:00+00:00",
            video_fetched_at="2026-07-18T00:00:00+00:00",
            is_stale=False,
            last_error=None,
        )

    def is_stale(self) -> bool:
        return False

    async def run_periodic_refresh(self, *, stop) -> None:
        self.periodic_started = True
        await stop.wait()


@dataclass
class FakeRunner:
    """Records the requests it is given and returns a scripted outcome."""

    db: Database
    events: EventBus
    outcome: GenerationOutcome | None = None
    issues: list = field(default_factory=list)
    requests: list[GenerationRequest] = field(default_factory=list)

    async def validate(self, request: GenerationRequest) -> list:
        return list(self.issues)

    def _default_outcome(self, request: GenerationRequest) -> GenerationOutcome:
        row = self.db.create_generation(
            project_id=request.project_id,
            kind=request.kind,
            model=request.model,
            prompt=request.prompt,
            params=request.params,
            state=GenerationState.COMPLETE,
        )
        return GenerationOutcome(
            generation_id=row.id,
            state=GenerationState.COMPLETE,
            file_path=None,
            asset_id=None,
            cost=Decimal("0.04"),
            error_reason=None,
            error_detail=None,
        )


class FakeImageRunner(FakeRunner):
    async def run(self, request: GenerationRequest) -> GenerationOutcome:
        self.requests.append(request)
        return self.outcome or self._default_outcome(request)


@dataclass
class FakeGate:
    """Records reservation releases.

    `resume_pending_jobs` reaches through the runner to `runner.gate.release`
    on both the orphaned and the timed-out branch, so a runner double without
    a gate raises AttributeError the moment those branches are exercised.
    """

    released: list[tuple[Any, Decimal | None, bool]] = field(default_factory=list)

    async def release(
        self,
        reservation,
        *,
        actual_cost: Decimal | None = None,
        succeeded: bool = False,
    ) -> None:
        self.released.append((reservation, actual_cost, succeeded))


class FakeVideoRunner(FakeRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pollers: dict[str, asyncio.Task] = {}
        self.shutdown_calls = 0
        self.gate = FakeGate()

    async def submit(self, request: GenerationRequest) -> GenerationOutcome:
        self.requests.append(request)
        if self.outcome is not None:
            return self.outcome
        row = self.db.create_generation(
            project_id=request.project_id,
            kind=request.kind,
            model=request.model,
            prompt=request.prompt,
            params=request.params,
            state=GenerationState.SUBMITTED,
        )
        return GenerationOutcome(
            generation_id=row.id,
            state=GenerationState.SUBMITTED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=None,
            error_detail=None,
        )

    def attach_poller(self, gen_id: str, *, reservation=None) -> asyncio.Task:
        task = self._pollers.get(gen_id)
        if task is None:
            task = asyncio.get_running_loop().create_future()
            self._pollers[gen_id] = task
        return task

    def active_pollers(self):
        return dict(self._pollers)

    async def shutdown(self, *, timeout_s: float = 10.0) -> None:
        self.shutdown_calls += 1
        for task in self._pollers.values():
            task.cancel()
        self._pollers.clear()


class FakeClient:
    """Stands in for OpenRouterClient where only the key call is needed."""

    def __init__(
        self, key_status: KeyStatus | None = None, error: Exception | None = None
    ):
        self._key_status = key_status or KeyStatus(
            limit=Decimal("100"),
            limit_remaining=Decimal("74.5"),
            limit_reset="monthly",
            usage=Decimal("25.5"),
            usage_daily=Decimal("25.5"),
            is_free_tier=False,
        )
        self._error = error

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_key_status(self) -> KeyStatus:
        if self._error is not None:
            raise self._error
        return self._key_status


def build_test_state(
    *,
    db: Database,
    paths: MediaPaths,
    settings: Settings | None = None,
    catalog: FakeCatalog | None = None,
    client: FakeClient | None = None,
) -> AppState:
    """Assemble an AppState from real store/budget objects and fake edges."""
    settings = settings or Settings(
        media_root=paths.root,
        db_path=paths.root / "unused.db",
        daily_cap_usd=None,
    )
    events = EventBus()
    ledger = Ledger(db)
    gate = BudgetGate(
        db,
        ledger,
        daily_cap_usd=settings.daily_cap_usd,
        max_job_cost_usd=settings.max_job_cost_usd,
        max_in_flight=settings.max_in_flight,
    )
    the_client = client or FakeClient()
    return AppState(
        settings=settings,
        db=db,
        paths=paths,
        catalog=catalog or FakeCatalog(),
        ledger=ledger,
        gate=gate,
        image_runner=FakeImageRunner(db=db, events=events),
        video_runner=FakeVideoRunner(db=db, events=events),
        events=events,
        resume_report=None,
        client_factory=lambda kind: the_client,
    )


def failed_outcome(
    db: Database, project_id: str, *, reason: ErrorReason, state: GenerationState
) -> GenerationOutcome:
    """A scripted rejection/failure outcome with a real row behind it."""
    row = db.create_generation(
        project_id=project_id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="x",
        params={},
        state=state,
    )
    return GenerationOutcome(
        generation_id=row.id,
        state=state,
        file_path=None,
        asset_id=None,
        cost=None,
        error_reason=reason,
        error_detail="scripted",
    )
