"""The model catalogue: fetched via orclient, persisted via store.

This class exists because neither neighbour can hold the cache: orclient has
no persistence and store has no network (spec 4.1). Every read is served from
SQLite; the network is touched only when the cache is empty or expired, and a
failed refresh always yields to the stale cache rather than emptying it.

Database calls here are sub-millisecond single-row metadata reads, so they run
inline rather than through anyio.to_thread; the threading rule in the
interface contract applies to the bulk queries in jobs/ and web/.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio

from higgshole.config import MediaKind, Settings
from higgshole.orclient.client import OpenRouterClient
from higgshole.orclient.types import ImageModel, VideoModel
from higgshole.store.db import Database, GenerationKind, utc_now_iso

#: settings-table key holding the last refresh failure (frozen contract).
LAST_ERROR_KEY = "catalog_last_refresh_error"


@dataclass(frozen=True)
class CatalogStatus:
    """What Settings shows the operator about catalogue freshness."""

    image_fetched_at: str | None
    video_fetched_at: str | None
    is_stale: bool
    last_error: str | None


def video_capabilities(model: VideoModel) -> dict[str, Any]:
    """Serialise a VideoModel back into its API payload shape.

    The catalogue is stored in the provider's own vocabulary rather than an
    internal one, so ``VideoModel.from_api`` is the single parser for both the
    live response and the cached row — there is no second shape to keep in
    sync.
    """
    return {
        "id": model.id,
        "supported_resolutions": list(model.supported_resolutions),
        "supported_aspect_ratios": list(model.supported_aspect_ratios),
        "supported_durations": list(model.supported_durations),
        "supported_sizes": list(model.supported_sizes),
        "supported_frame_images": list(model.supported_frame_images),
        "generate_audio": model.generate_audio,
        "seed": model.seed,
        "pricing_skus": dict(model.pricing_skus),
        "allowed_passthrough_parameters": list(model.allowed_passthrough_parameters),
    }


def image_capabilities(model: ImageModel) -> dict[str, Any]:
    """Serialise an ImageModel back into its API payload shape."""
    return {
        "id": model.id,
        "name": model.name,
        "supports_streaming": model.supports_streaming,
        "supported_parameters": {
            "input_references": {
                "type": "range",
                "min": 0,
                "max": model.max_input_references,
            },
            "quality": {"type": "enum", "values": list(model.quality_values)},
            "n": {"type": "range", "min": 1, "max": model.max_n},
        },
    }


class CatalogCache:
    """Owns the model catalogue."""

    def __init__(
        self,
        db: Database,
        client_factory: Callable[[MediaKind], OpenRouterClient],
        *,
        ttl_hours: int = 24,
    ) -> None:
        """`client_factory` takes the media kind and returns a fresh client,
        so the cache never captures a key that Settings may rotate, and each
        catalogue is fetched with the key configured for its own kind.
        """
        self._db = db
        self._client_factory = client_factory
        self._ttl_hours = ttl_hours

    @classmethod
    def from_settings(cls, db: Database, settings: Settings) -> CatalogCache:
        def factory(kind: MediaKind) -> OpenRouterClient:
            # Per-kind selection (spec section 7): an or-chain across the three
            # keys would fetch the image catalogue with the video key whenever
            # only HIGGSHOLE_OPENROUTER_API_KEY_VIDEO is set.
            # A blank key raises AuthError here, recorded as the catalogue
            # status rather than surfacing as a transport error later.
            return OpenRouterClient(settings.openrouter_api_key_for(kind) or "")

        return cls(db, factory, ttl_hours=settings.catalog_ttl_hours)

    @property
    def ttl_hours(self) -> int:
        return self._ttl_hours

    @property
    def client_factory(self) -> Callable[[MediaKind], OpenRouterClient]:
        return self._client_factory

    # -- freshness ------------------------------------------------------

    def _expired(self, fetched_at: str | None) -> bool:
        if not fetched_at:
            return True
        try:
            when = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return datetime.now(UTC) - when >= timedelta(hours=self._ttl_hours)

    def is_stale(self) -> bool:
        """True when either catalogue is missing or older than ttl_hours."""
        return any(
            self._expired(self._db.catalog_fetched_at(kind)) for kind in GenerationKind
        )

    def status(self) -> CatalogStatus:
        """Read-only freshness report; no I/O beyond the database."""
        return CatalogStatus(
            image_fetched_at=self._db.catalog_fetched_at(GenerationKind.IMAGE),
            video_fetched_at=self._db.catalog_fetched_at(GenerationKind.VIDEO),
            is_stale=self.is_stale(),
            last_error=self._db.get_setting(LAST_ERROR_KEY),
        )

    # -- reads ----------------------------------------------------------

    async def _ensure_fresh(self, kind: GenerationKind) -> None:
        if self._db.list_catalog(kind) and not self._expired(
            self._db.catalog_fetched_at(kind)
        ):
            return
        await self.refresh()

    async def get_video_models(self) -> tuple[VideoModel, ...]:
        """Cached video models, refreshing first when empty or expired.

        On refresh failure the stale cache is served: an out-of-date
        capability list is far more useful than none (spec 4.2).
        """
        await self._ensure_fresh(GenerationKind.VIDEO)
        return tuple(
            VideoModel.from_api(row.capabilities)
            for row in self._db.list_catalog(GenerationKind.VIDEO)
        )

    async def get_image_models(self) -> tuple[ImageModel, ...]:
        await self._ensure_fresh(GenerationKind.IMAGE)
        return tuple(
            ImageModel.from_api(row.capabilities)
            for row in self._db.list_catalog(GenerationKind.IMAGE)
        )

    async def get_video_model(self, model_id: str) -> VideoModel | None:
        for model in await self.get_video_models():
            if model.id == model_id:
                return model
        return None

    async def get_image_model(self, model_id: str) -> ImageModel | None:
        for model in await self.get_image_models():
            if model.id == model_id:
                return model
        return None

    async def get_image_pricing(self, model_id: str) -> list[dict[str, Any]]:
        """Image pricing line items, fetched lazily on first use of a model.

        Eager fetching would mean roughly 38 requests at boot (spec 4.2).
        Returns [] when the fetch fails and nothing is cached — never a
        fabricated price.
        """
        cached = self._db.get_pricing(model_id)
        if cached is not None and not self._expired(cached.fetched_at):
            return cached.pricing

        try:
            async with self._client_factory("image") as client:
                pricing = await client.get_image_model_pricing(model_id)
        except Exception as exc:  # noqa: BLE001 - never propagate to a page render
            self._db.set_setting(LAST_ERROR_KEY, f"pricing {model_id}: {exc}")
            return cached.pricing if cached is not None else []

        self._db.upsert_pricing(
            model_id=model_id, pricing=pricing, fetched_at=utc_now_iso()
        )
        return pricing

    # -- writes ---------------------------------------------------------

    async def _refresh_kind(self, kind: GenerationKind, *, force: bool) -> str | None:
        """Refresh one kind. Returns an error message, or None."""
        if not force and not self._expired(self._db.catalog_fetched_at(kind)):
            return None

        try:
            async with self._client_factory(kind.value) as client:
                if kind is GenerationKind.VIDEO:
                    entries = [
                        (model.id, video_capabilities(model))
                        for model in await client.list_video_models()
                    ]
                else:
                    entries = [
                        (model.id, image_capabilities(model))
                        for model in await client.list_image_models()
                    ]
        except Exception as exc:  # noqa: BLE001 - startup must not block on this
            return f"{kind.value}: {exc}"

        # Only replace once the whole list is in hand, so a partial fetch can
        # never half-overwrite a good cache.
        self._db.replace_catalog(kind, entries, fetched_at=utc_now_iso())
        return None

    async def refresh(self, *, force: bool = False) -> CatalogStatus:
        """Refresh both catalogues. Never raises on a provider failure."""
        errors = [
            message
            for kind in (GenerationKind.VIDEO, GenerationKind.IMAGE)
            if (message := await self._refresh_kind(kind, force=force)) is not None
        ]

        if errors:
            self._db.set_setting(LAST_ERROR_KEY, "; ".join(errors))
        else:
            self._db.delete_setting(LAST_ERROR_KEY)

        return self.status()

    async def refresh_if_stale(self) -> CatalogStatus:
        if not self.is_stale():
            return self.status()
        return await self.refresh()

    async def run_periodic_refresh(self, *, stop: anyio.Event) -> None:
        """Refresh every ttl_hours until `stop` is set.

        Started by web/app.py's lifespan; never started by tests.
        """
        interval = self._ttl_hours * 3600
        while not stop.is_set():
            with anyio.move_on_after(interval):
                await stop.wait()
            if stop.is_set():
                return
            await self.refresh(force=True)
