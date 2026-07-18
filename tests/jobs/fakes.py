"""Test doubles for the job engine.

No test in this package ever constructs a real OpenRouterClient, opens a
socket, or sleeps for a real duration.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

from higgshole.budget.gate import BudgetGate
from higgshole.budget.ledger import Ledger
from higgshole.config import Settings
from higgshole.jobs.events import JobEvent
from higgshole.jobs.runner import GenerationRequest, RetryPolicy
from higgshole.orclient.types import ImageModel, ImageResult, VideoJob, VideoModel
from higgshole.store.db import (
    AssetKind,
    Database,
    GenerationKind,
    InputRole,
)
from higgshole.store.metadata import MediaMetadata
from higgshole.store.paths import MediaPaths

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stub-pixels"
MP4_BYTES = b"\x00\x00\x00 ftypmp42" + b"stub-frames"

IMAGE_MODEL = ImageModel.from_api(
    {
        "id": "test/image-model",
        "name": "Test Image Model",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["low", "medium", "high"]},
            "n": {"type": "range", "min": 1, "max": 10},
            "input_references": {"type": "range", "min": 0, "max": 4},
        },
    }
)

VIDEO_MODEL = VideoModel.from_api(
    {
        "id": "test/video-model",
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_durations": [4, 8],
        "supported_frame_images": ["first_frame", "last_frame"],
        "generate_audio": False,
        "seed": True,
        # Token-priced on purpose: an unestimable model is the interesting
        # case, because it forces the pessimistic reservation path.
        "pricing_skus": {"video_tokens": "0.000007"},
    }
)


class FakeClock:
    """A clock that advances only when something sleeps or a test says so."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += seconds
        # Yield so other tasks make progress, exactly as a real sleep would.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._now += seconds


class RecordingPublisher:
    """Captures every JobEvent so tests can assert on the transition sequence."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []

    def publish(self, event: JobEvent) -> None:
        self.events.append(event)

    def states_for(self, generation_id: str) -> list[str]:
        return [
            str(event.state)
            for event in self.events
            if event.generation_id == generation_id
        ]


class FakeOpenRouterClient:
    """A scripted stand-in for OpenRouterClient.

    Every queue holds either a value to return or an exception to raise, so a
    test expresses "429 then success" as a two-item list.
    """

    def __init__(
        self,
        *,
        image_results: list[Any] | None = None,
        submit_results: list[Any] | None = None,
        poll_results: list[Any] | None = None,
        download_results: list[Any] | None = None,
    ) -> None:
        self.image_results = list(image_results or [])
        self.submit_results = list(submit_results or [])
        self.poll_results = list(poll_results or [])
        self.download_results = list(download_results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    async def __aenter__(self) -> FakeOpenRouterClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed += 1

    async def aclose(self) -> None:
        self.closed += 1

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last_call(self, name: str) -> dict[str, Any]:
        for call_name, payload in reversed(self.calls):
            if call_name == name:
                return payload
        raise AssertionError(f"{name} was never called")

    @staticmethod
    def _next(queue: list[Any], label: str) -> Any:
        if not queue:
            raise AssertionError(f"fake client ran out of scripted {label} results")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        input_references: Any = (),
        **params: Any,
    ) -> ImageResult:
        self.calls.append(
            (
                "generate_image",
                {
                    "model": model,
                    "prompt": prompt,
                    "input_references": list(input_references),
                    **params,
                },
            )
        )
        return self._next(self.image_results, "image")

    async def submit_video(
        self,
        *,
        model: str,
        prompt: str,
        frame_images: Any = (),
        input_references: Any = (),
        **params: Any,
    ) -> VideoJob:
        self.calls.append(
            (
                "submit_video",
                {
                    "model": model,
                    "prompt": prompt,
                    "frame_images": list(frame_images),
                    "input_references": list(input_references),
                    **params,
                },
            )
        )
        return self._next(self.submit_results, "submit")

    async def get_video_job(self, job_id: str) -> VideoJob:
        self.calls.append(("get_video_job", {"job_id": job_id}))
        return self._next(self.poll_results, "poll")

    async def download_video(self, job_id: str, *, index: int = 0) -> bytes:
        self.calls.append(("download_video", {"job_id": job_id, "index": index}))
        return self._next(self.download_results, "download")


class FakeCatalog:
    """The CatalogCache surface the runners actually use."""

    def __init__(
        self,
        *,
        image_models: tuple[ImageModel, ...] = (IMAGE_MODEL,),
        video_models: tuple[VideoModel, ...] = (VIDEO_MODEL,),
        pricing: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._image = {model.id: model for model in image_models}
        self._video = {model.id: model for model in video_models}
        self._pricing = pricing or {}

    async def get_image_models(self) -> tuple[ImageModel, ...]:
        return tuple(self._image.values())

    async def get_video_models(self) -> tuple[VideoModel, ...]:
        return tuple(self._video.values())

    async def get_image_model(self, model_id: str) -> ImageModel | None:
        return self._image.get(model_id)

    async def get_video_model(self, model_id: str) -> VideoModel | None:
        return self._video.get(model_id)

    async def get_image_pricing(self, model_id: str) -> list[dict[str, Any]]:
        return self._pricing.get(model_id, [])


def fake_metadata_for(path: Path) -> MediaMetadata:
    """Stand in for probe_media without requiring ffprobe or a real codec."""
    if path.suffix == ".mp4":
        return MediaMetadata(
            mime_type="video/mp4",
            bytes=path.stat().st_size,
            width=1280,
            height=720,
            duration_s=4.0,
            embedded_params={},
        )
    return MediaMetadata(
        mime_type="image/png",
        bytes=path.stat().st_size,
        width=4,
        height=4,
        duration_s=None,
        embedded_params={},
    )


def fake_thumbnail(source: Path, destination: Path, **kwargs: Any) -> MediaMetadata:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"RIFF-stub-webp")
    return MediaMetadata(
        mime_type="image/webp",
        bytes=destination.stat().st_size,
        width=64,
        height=64,
        duration_s=None,
        embedded_params={},
    )


class Harness:
    """A fully wired job engine over a temporary directory and in-memory DB."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        daily_cap_usd: Decimal | None = None,
        max_in_flight: int = 3,
        job_timeout_minutes: int = 30,
        poll_interval_seconds: int = 5,
        max_retries: int = 2,
        client: FakeOpenRouterClient | None = None,
        catalog: FakeCatalog | None = None,
    ) -> None:
        self.settings = Settings(
            openrouter_api_key="sk-or-v1-test",
            openrouter_api_key_image=None,
            openrouter_api_key_video=None,
            media_root=tmp_path / "media",
            db_path=tmp_path / "state" / "higgshole.db",
            daily_cap_usd=daily_cap_usd,
            max_job_cost_usd=Decimal("2.00"),
            max_in_flight=max_in_flight,
            job_timeout_minutes=job_timeout_minutes,
            poll_interval_seconds=poll_interval_seconds,
            max_retries=max_retries,
            catalog_ttl_hours=24,
            reference_transport="data_uri",
        )
        self.db = Database.in_memory()
        self.db.migrate()
        self.project = self.db.ensure_default_project()

        self.paths = MediaPaths(self.settings.media_root)
        self.paths.ensure_project_tree(self.project.slug)

        self.ledger = Ledger(self.db)
        self.gate = BudgetGate.from_settings(self.db, self.ledger, self.settings)
        self.catalog = catalog or FakeCatalog()
        self.events = RecordingPublisher()
        self.clock = FakeClock()
        self.client = client or FakeOpenRouterClient()

        # Imported here so this module stays importable before Task 5 lands.
        from higgshole.jobs.runner import ImageJobRunner, VideoJobRunner

        common = {
            "db": self.db,
            "paths": self.paths,
            "gate": self.gate,
            "catalog": self.catalog,
            "settings": self.settings,
            "client_factory": lambda kind: self.client,
            "events": self.events,
            "clock": self.clock,
            "retry_policy": RetryPolicy(
                max_retries=max_retries, base_delay_s=1.0, max_delay_s=4.0
            ),
        }
        self.image_runner = ImageJobRunner(**common)
        self.video_runner = VideoJobRunner(**common)

    # -- request builders -------------------------------------------------

    def image_request(self, **overrides: Any) -> GenerationRequest:
        fields: dict[str, Any] = {
            "kind": GenerationKind.IMAGE,
            "project_id": self.project.id,
            "project_slug": self.project.slug,
            "model": IMAGE_MODEL.id,
            "prompt": "neon city street at night, rain",
            "params": {"quality": "high", "output_format": "png"},
            "inputs": (),
        }
        fields.update(overrides)
        return GenerationRequest(**fields)

    def video_request(self, **overrides: Any) -> GenerationRequest:
        fields: dict[str, Any] = {
            "kind": GenerationKind.VIDEO,
            "project_id": self.project.id,
            "project_slug": self.project.slug,
            "model": VIDEO_MODEL.id,
            "prompt": "drone over a coastline",
            "params": {"duration": 4, "resolution": "720p"},
            "inputs": (),
        }
        fields.update(overrides)
        return GenerationRequest(**fields)

    def upload(self, name: str = "ref.png", data: bytes = PNG_BYTES) -> str:
        """Ingest a file as an upload asset and return its asset ID."""
        target = self.paths.uploads_dir(self.project.slug) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        relative = target.relative_to(self.paths.root).as_posix()
        asset = self.db.create_asset(
            kind=AssetKind.UPLOAD,
            file_path=relative,
            mime_type="image/png",
            bytes_=len(data),
            width=4,
            height=4,
        )
        return asset.id

    def image_result(self, cost: str | None = "0.04") -> ImageResult:
        return ImageResult(
            data=PNG_BYTES,
            media_type="image/png",
            cost=None if cost is None else Decimal(cost),
        )

    def ledger_total(self, gen_id: str) -> Decimal:
        return sum(
            (row.amount for row in self.db.list_ledger_for_generation(gen_id)),
            Decimal("0"),
        )


def video_job(
    job_id: str = "job-1",
    status: str = "pending",
    *,
    cost: str | None = None,
    error: str | None = None,
    urls: tuple[str, ...] = (),
) -> VideoJob:
    return VideoJob(
        id=job_id,
        status=status,
        generation_id="gen-provider-1",
        result_urls=urls,
        cost=None if cost is None else Decimal(cost),
        error=error,
    )


__all__ = [
    "IMAGE_MODEL",
    "MP4_BYTES",
    "PNG_BYTES",
    "VIDEO_MODEL",
    "FakeCatalog",
    "FakeClock",
    "FakeOpenRouterClient",
    "Harness",
    "InputRole",
    "RecordingPublisher",
    "fake_metadata_for",
    "fake_thumbnail",
    "video_job",
]
