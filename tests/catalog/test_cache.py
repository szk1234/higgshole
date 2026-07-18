from datetime import UTC, datetime, timedelta

import pytest

from higgshole.catalog.cache import CatalogCache
from higgshole.config import Settings
from higgshole.orclient.errors import ProviderError
from higgshole.orclient.types import ImageModel, VideoModel
from higgshole.store.db import Database, GenerationKind

VEO = VideoModel.from_api(
    {
        "id": "google/veo-3.1",
        "supported_durations": [4, 6, 8],
        "supported_resolutions": ["720p", "1080p"],
        "supported_frame_images": ["first_frame", "last_frame"],
        "generate_audio": True,
        "seed": True,
        "pricing_skus": {"duration_seconds_with_audio": "0.40"},
        "allowed_passthrough_parameters": ["negative_prompt"],
    }
)

SORA = VideoModel.from_api({"id": "openai/sora-2-pro", "supported_durations": [4, 8]})

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
        "name": "GPT Image 2",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["auto", "low", "high"]},
            "n": {"type": "range", "min": 1, "max": 10},
            "input_references": {"type": "range", "min": 0, "max": 16},
        },
        "supports_streaming": True,
    }
)

PRICING = [{"billable": "output_image", "unit": "image", "cost_usd": 0.04}]


class FakeClient:
    """Stands in for OpenRouterClient. Makes no network call of any kind."""

    def __init__(self, owner):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_video_models(self):
        self._owner.video_calls += 1
        if self._owner.fail:
            raise ProviderError("upstream down", status_code=503)
        return tuple(self._owner.video)

    async def list_image_models(self):
        self._owner.image_calls += 1
        if self._owner.fail:
            raise ProviderError("upstream down", status_code=503)
        return tuple(self._owner.image)

    async def get_image_model_pricing(self, model_id):
        self._owner.pricing_calls += 1
        if self._owner.fail:
            raise ProviderError("upstream down", status_code=503)
        return list(self._owner.pricing)


class Provider:
    def __init__(self, *, video=(VEO, SORA), image=(GPT_IMAGE,), pricing=PRICING):
        self.video = list(video)
        self.image = list(image)
        self.pricing = list(pricing)
        self.fail = False
        self.video_calls = 0
        self.image_calls = 0
        self.pricing_calls = 0
        self.kinds = []

    def __call__(self, kind):
        # Records the media kind so tests can assert the cache asks for a
        # client per kind rather than reusing one key for both catalogues.
        self.kinds.append(kind)
        return FakeClient(self)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def cache(db, provider):
    return CatalogCache(db, provider, ttl_hours=24)


def stamp(hours_ago):
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


async def test_refresh_populates_both_catalogues(cache, db):
    await cache.refresh()

    assert {r.model_id for r in db.list_catalog(GenerationKind.VIDEO)} == {
        "google/veo-3.1",
        "openai/sora-2-pro",
    }
    assert [r.model_id for r in db.list_catalog(GenerationKind.IMAGE)] == [
        "openai/gpt-image-2"
    ]


async def test_get_video_models_reads_from_the_cache_without_fetching(cache, provider):
    await cache.refresh()
    provider.video_calls = 0

    models = await cache.get_video_models()

    assert provider.video_calls == 0
    assert [m.id for m in models] == ["google/veo-3.1", "openai/sora-2-pro"]
    assert models[0].pricing_skus["duration_seconds_with_audio"] == "0.40"
    assert models[0].supported_frame_images == ("first_frame", "last_frame")


async def test_get_video_models_refreshes_an_empty_cache(cache, provider):
    models = await cache.get_video_models()

    assert provider.video_calls == 1
    assert len(models) == 2


async def test_stale_cache_is_refreshed(db, provider):
    db.replace_catalog(
        GenerationKind.VIDEO, [("old/model", {"id": "old/model"})], fetched_at=stamp(48)
    )
    cache = CatalogCache(db, provider, ttl_hours=24)

    models = await cache.get_video_models()

    assert provider.video_calls == 1
    assert "old/model" not in {m.id for m in models}


async def test_a_failed_refresh_serves_the_stale_cache(db, provider):
    # Spec section 4.2: a refresh failure must never empty a good cache.
    db.replace_catalog(
        GenerationKind.VIDEO, [("old/model", {"id": "old/model"})], fetched_at=stamp(48)
    )
    provider.fail = True
    cache = CatalogCache(db, provider, ttl_hours=24)

    models = await cache.get_video_models()

    assert [m.id for m in models] == ["old/model"]


async def test_a_failed_refresh_records_the_error(cache, db, provider):
    provider.fail = True

    status = await cache.refresh()

    assert status.last_error is not None
    assert db.get_setting("catalog_last_refresh_error") is not None


async def test_a_successful_refresh_clears_a_previous_error(cache, db, provider):
    provider.fail = True
    await cache.refresh()
    provider.fail = False

    status = await cache.refresh(force=True)

    assert status.last_error is None
    assert db.get_setting("catalog_last_refresh_error") is None


async def test_refresh_never_raises_on_a_provider_failure(cache, provider):
    # Startup must not block on catalogue availability.
    provider.fail = True

    status = await cache.refresh()

    assert status.is_stale is True


async def test_force_refresh_ignores_the_ttl(cache, provider):
    await cache.refresh()
    provider.video_calls = 0

    await cache.refresh()
    assert provider.video_calls == 0

    await cache.refresh(force=True)
    assert provider.video_calls == 1


async def test_get_video_model_by_id(cache):
    model = await cache.get_video_model("google/veo-3.1")

    assert model is not None
    assert model.generate_audio is True
    assert await cache.get_video_model("absent/model") is None


async def test_get_image_model_by_id_returns_none_when_absent(cache):
    model = await cache.get_image_model("openai/gpt-image-2")

    assert model is not None
    assert model.max_input_references == 16
    assert model.quality_values == ("auto", "low", "high")
    assert await cache.get_image_model("absent/model") is None


async def test_image_pricing_is_fetched_lazily_and_then_cached(cache, provider):
    # Spec section 4.2: eager fetching would mean ~38 requests at boot.
    first = await cache.get_image_pricing("openai/gpt-image-2")
    second = await cache.get_image_pricing("openai/gpt-image-2")

    assert first == PRICING == second
    assert provider.pricing_calls == 1


async def test_image_pricing_returns_empty_when_the_fetch_fails_and_nothing_is_cached(
    cache, provider
):
    provider.fail = True

    assert await cache.get_image_pricing("openai/gpt-image-2") == []


async def test_image_pricing_serves_the_cache_when_the_fetch_fails(db, provider):
    db.upsert_pricing(
        model_id="openai/gpt-image-2", pricing=PRICING, fetched_at=stamp(48)
    )
    provider.fail = True
    cache = CatalogCache(db, provider, ttl_hours=24)

    assert await cache.get_image_pricing("openai/gpt-image-2") == PRICING


async def test_status_reports_freshness(cache):
    await cache.refresh()

    status = cache.status()

    assert status.is_stale is False
    assert status.video_fetched_at is not None
    assert status.image_fetched_at is not None
    assert status.last_error is None


def test_is_stale_when_one_kind_is_missing(db, provider):
    db.replace_catalog(
        GenerationKind.VIDEO, [("a/b", {"id": "a/b"})], fetched_at=stamp(0)
    )

    assert CatalogCache(db, provider, ttl_hours=24).is_stale() is True


def test_from_settings_builds_a_factory_without_capturing_a_stale_key(db, monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("HIGGSHOLE_CATALOG_TTL_HOURS", "6")

    monkeypatch.setattr(
        "higgshole.catalog.cache.OpenRouterClient", lambda key: object()
    )

    cache = CatalogCache.from_settings(db, Settings())

    assert cache.ttl_hours == 6
    client = cache.client_factory("image")
    assert client is not cache.client_factory("image")


def test_from_settings_uses_the_key_configured_for_each_kind(db, monkeypatch):
    # Spec section 7: with only the video key set, the image catalogue must not
    # borrow it — each kind resolves its own key (falling back to the shared one).
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY_IMAGE", raising=False)
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY_VIDEO", "sk-or-v1-video")
    keys = []
    monkeypatch.setattr(
        "higgshole.catalog.cache.OpenRouterClient", lambda key: keys.append(key)
    )

    cache = CatalogCache.from_settings(db, Settings())
    cache.client_factory("video")
    cache.client_factory("image")

    assert keys == ["sk-or-v1-video", ""]


async def test_each_catalogue_is_fetched_with_its_own_kind(cache, provider):
    await cache.refresh()

    assert provider.kinds == ["video", "image"]
