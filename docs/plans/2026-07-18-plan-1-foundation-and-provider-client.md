# HiggsHole Plan 1 — Foundation & Provider Client

> **How to execute this plan:** work through it strictly task by task, in order.
> Each task is self-contained and ends with a passing test suite and a commit,
> so it is a natural review checkpoint — do not start the next task until the
> current one is green. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Every task follows the same cycle: write a failing test, run it to confirm it
> fails for the reason you expect, write the minimal implementation, confirm it
> passes, commit. Do not write implementation before its test.

**Goal:** Build the project scaffolding, configuration layer, a fully typed OpenRouter client, and the model capability catalogue — all tested against recorded fixtures with no network access and no spend.

**Architecture:** A `src/`-layout Python package. `orclient/` is a pure HTTP client that never touches disk or database; `catalog/` normalises the two dissimilar model catalogues (images and videos) into one internal shape and owns parameter validation. Everything is tested with `respx`, which intercepts `httpx` at the transport layer, so the suite runs offline and free.

**Tech Stack:** Python 3.12+, `uv`, `httpx`, `pydantic`, `pytest`, `pytest-asyncio`, `respx`.

**Source specification:** `docs/specs/2026-07-18-higgshole-design.md`

## Global Constraints

- **Python 3.12+.** Use `StrEnum` (3.11+) and PEP 604 unions freely.
- **Public repository.** No committed file may contain a personal name, an employer name, a machine-specific absolute path, or an API key. Defaults use XDG paths.
- **`orclient/` must never import from `store/`, must never touch the filesystem, and must never open a database connection.** This is what keeps it testable without spend.
- **No test may make a real network request.** Every HTTP interaction is intercepted by `respx`.
- **All configuration is environment variables** prefixed `HIGGSHOLE_`, with the defaults in spec §8. Never hardcode a deployment path.
- **Terminal job statuses are exactly:** `completed`, `failed`, `cancelled`, `expired`. Any *unrecognised* status is non-terminal — continue polling (spec §2.4).
- **Never fabricate a cost.** Where a cost cannot be computed, return `None`, never `0` (spec §3.3, §3.4).
- Commit after every task. Conventional commit prefixes (`feat:`, `test:`, `chore:`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Project metadata, dependencies, pytest and ruff configuration |
| `src/higgshole/__init__.py` | Package marker, version |
| `src/higgshole/config.py` | Environment-variable settings with XDG defaults |
| `src/higgshole/orclient/__init__.py` | Public re-exports for the client package |
| `src/higgshole/orclient/errors.py` | Typed exception hierarchy mapped from HTTP responses |
| `src/higgshole/orclient/types.py` | Frozen dataclasses for catalogue entries, results, jobs |
| `src/higgshole/orclient/client.py` | The `OpenRouterClient` — all HTTP calls |
| `src/higgshole/catalog/__init__.py` | Public re-exports |
| `src/higgshole/catalog/validation.py` | Parameter validation and the advisory/hard precedence rule |
| `tests/conftest.py` | Shared fixtures |
| `tests/fixtures/*.json` | Recorded API payloads |
| `tests/test_config.py` | Configuration tests |
| `tests/orclient/test_errors.py` | Error mapping tests |
| `tests/orclient/test_catalog_fetch.py` | Catalogue fetch and parsing |
| `tests/orclient/test_images.py` | Image generation |
| `tests/orclient/test_videos.py` | Video submit, poll, download |
| `tests/orclient/test_key.py` | Key validation and budget figures |
| `tests/catalog/test_validation.py` | Validation rules |

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/higgshole/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an installed package `higgshole` with `__version__: str`; a working `pytest` invocation via `uv run pytest`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_smoke.py`:

```python
def test_package_importable():
    import higgshole

    assert higgshole.__version__ == "0.1.0"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_smoke.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole'` (or `uv` reports no project found, since `pyproject.toml` does not yet exist).

- [ ] **Step 3: Create the project definition**

Create `pyproject.toml`:

```toml
[project]
name = "higgshole"
version = "0.1.0"
description = "Self-hosted AI image and video generation console backed by OpenRouter"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/higgshole"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

Create `src/higgshole/__init__.py`:

```python
"""HiggsHole — self-hosted AI image and video generation console."""

__version__ = "0.1.0"
```

Create an empty `tests/__init__.py`.

- [ ] **Step 4: Install and run the test**

Run: `uv sync --extra dev && uv run pytest tests/test_smoke.py -v`

Expected: PASS — `1 passed`.

- [ ] **Step 5: Ignore the lockfile decision and commit**

`uv.lock` **should** be committed for an application (it pins the exact dependency set for reproducible deploys). Confirm `.gitignore` does not exclude it:

Run: `git check-ignore uv.lock || echo "uv.lock will be tracked — correct"`

Expected: `uv.lock will be tracked — correct`

```bash
git add pyproject.toml uv.lock src/higgshole/__init__.py tests/__init__.py tests/test_smoke.py
git commit -m "chore: scaffold project with uv, pytest and ruff"
```

---

## Task 2: Configuration

**Files:**
- Create: `src/higgshole/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` (a `pydantic_settings.BaseSettings` subclass) with the fields below, and `get_settings() -> Settings`. Later tasks read `settings.openrouter_api_key_for(kind)`.

Field names and defaults come from spec §8 and must match exactly:

| Field | Type | Default |
|---|---|---|
| `openrouter_api_key` | `str \| None` | `None` |
| `openrouter_api_key_image` | `str \| None` | `None` |
| `openrouter_api_key_video` | `str \| None` | `None` |
| `media_root` | `Path` | `${XDG_DATA_HOME:-~/.local/share}/higgshole/media` |
| `db_path` | `Path` | `${XDG_STATE_HOME:-~/.local/state}/higgshole/higgshole.db` |
| `bind_host` | `str` | `127.0.0.1` |
| `bind_port` | `int` | `8077` |
| `daily_cap_usd` | `Decimal \| None` | `None` |
| `max_job_cost_usd` | `Decimal` | `2.00` |
| `max_in_flight` | `int` | `3` |
| `job_timeout_minutes` | `int` | `30` |
| `poll_interval_seconds` | `int` | `5` |
| `max_retries` | `int` | `3` |
| `catalog_ttl_hours` | `int` | `24` |
| `reference_transport` | `str` | `data_uri` |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
from decimal import Decimal
from pathlib import Path

import pytest

from higgshole.config import Settings


def test_defaults_use_xdg_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for var in ("HIGGSHOLE_MEDIA_ROOT", "HIGGSHOLE_DB_PATH"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings()

    assert settings.media_root == tmp_path / "data" / "higgshole" / "media"
    assert settings.db_path == tmp_path / "state" / "higgshole" / "higgshole.db"


def test_defaults_fall_back_to_home_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("HIGGSHOLE_MEDIA_ROOT", raising=False)

    settings = Settings()

    assert settings.media_root == Path.home() / ".local/share/higgshole/media"


def test_binds_to_loopback_by_default(monkeypatch):
    monkeypatch.delenv("HIGGSHOLE_BIND_HOST", raising=False)

    assert Settings().bind_host == "127.0.0.1"


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_BIND_PORT", "9000")
    monkeypatch.setenv("HIGGSHOLE_DAILY_CAP_USD", "12.50")

    settings = Settings()

    assert settings.bind_port == 9000
    assert settings.daily_cap_usd == Decimal("12.50")


def test_no_daily_cap_by_default(monkeypatch):
    monkeypatch.delenv("HIGGSHOLE_DAILY_CAP_USD", raising=False)

    assert Settings().daily_cap_usd is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("image", "sk-or-v1-img"), ("video", "sk-or-v1-vid")],
)
def test_per_kind_key_selection(monkeypatch, kind, expected):
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY", "sk-or-v1-default")
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY_IMAGE", "sk-or-v1-img")
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY_VIDEO", "sk-or-v1-vid")

    assert Settings().openrouter_api_key_for(kind) == expected


def test_per_kind_key_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_OPENROUTER_API_KEY", "sk-or-v1-default")
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY_IMAGE", raising=False)
    monkeypatch.delenv("HIGGSHOLE_OPENROUTER_API_KEY_VIDEO", raising=False)

    assert Settings().openrouter_api_key_for("image") == "sk-or-v1-default"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.config'`.

- [ ] **Step 3: Implement the settings module**

Create `src/higgshole/config.py`:

```python
"""Environment-driven configuration.

Defaults follow the XDG Base Directory specification so that a fresh clone
runs unprivileged with no setup. Deployment overrides everything explicitly.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MediaKind = Literal["image", "video"]


def _xdg(env_var: str, fallback: str) -> Path:
    """Resolve an XDG base directory, falling back to a path under $HOME."""
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / fallback


def _default_media_root() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "higgshole" / "media"


def _default_db_path() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "higgshole" / "higgshole.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIGGSHOLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openrouter_api_key: str | None = None
    openrouter_api_key_image: str | None = None
    openrouter_api_key_video: str | None = None

    media_root: Path = Field(default_factory=_default_media_root)
    db_path: Path = Field(default_factory=_default_db_path)

    bind_host: str = "127.0.0.1"
    bind_port: int = 8077

    daily_cap_usd: Decimal | None = None
    max_job_cost_usd: Decimal = Decimal("2.00")
    max_in_flight: int = 3

    job_timeout_minutes: int = 30
    poll_interval_seconds: int = 5
    max_retries: int = 3
    catalog_ttl_hours: int = 24

    reference_transport: str = "data_uri"

    @field_validator("media_root", "db_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    def openrouter_api_key_for(self, kind: MediaKind) -> str | None:
        """Return the key for a media kind, falling back to the shared default."""
        specific = {
            "image": self.openrouter_api_key_image,
            "video": self.openrouter_api_key_video,
        }[kind]
        return specific or self.openrouter_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_config.py -v`

Expected: PASS — `8 passed`.

> If `test_defaults_fall_back_to_home_when_xdg_unset` fails because your shell exports `XDG_DATA_HOME`, that is the test doing its job — `monkeypatch.delenv` handles it. A real failure here means `_xdg` is reading the variable at import time rather than call time.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/config.py tests/test_config.py
git commit -m "feat: add environment-driven configuration with XDG defaults"
```

---

## Task 3: Typed error hierarchy

**Files:**
- Create: `src/higgshole/orclient/__init__.py`
- Create: `src/higgshole/orclient/errors.py`
- Create: `tests/orclient/__init__.py`
- Create: `tests/orclient/test_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `OpenRouterError` and subclasses; `error_from_response(status_code: int, body: dict | None) -> OpenRouterError`. Later tasks raise these; the web layer maps them to the rows in spec §10.

- [ ] **Step 1: Write the failing tests**

Create `tests/orclient/__init__.py` (empty) and `tests/orclient/test_errors.py`:

```python
import pytest

from higgshole.orclient.errors import (
    AuthError,
    IndeterminateError,
    InsufficientCreditsError,
    InvalidRequestError,
    ModerationError,
    OpenRouterError,
    ProviderError,
    RateLimitError,
    error_from_response,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, InvalidRequestError),
        (401, AuthError),
        (402, InsufficientCreditsError),
        (429, RateLimitError),
        (500, ProviderError),
        (502, ProviderError),
    ],
)
def test_status_codes_map_to_types(status, expected):
    error = error_from_response(status, {"error": {"message": "boom", "code": status}})

    assert isinstance(error, expected)
    assert error.status_code == status
    assert "boom" in str(error)


def test_moderation_refusal_is_distinct_from_generic_bad_request():
    error = error_from_response(
        400, {"error": {"message": "Content policy violation", "code": 400}}
    )

    assert isinstance(error, ModerationError)


def test_unparseable_body_still_yields_an_error():
    error = error_from_response(503, None)

    assert isinstance(error, ProviderError)
    assert error.status_code == 503


def test_all_errors_share_a_base_type():
    for error_type in (
        AuthError,
        IndeterminateError,
        InsufficientCreditsError,
        InvalidRequestError,
        ModerationError,
        ProviderError,
        RateLimitError,
    ):
        assert issubclass(error_type, OpenRouterError)


def test_indeterminate_error_records_that_a_charge_may_have_occurred():
    error = IndeterminateError("connection reset after submit")

    assert error.may_have_charged is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_errors.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.orclient'`.

- [ ] **Step 3: Implement the errors module**

Create an empty `src/higgshole/orclient/__init__.py` for now (Task 7 populates it).

Create `src/higgshole/orclient/errors.py`:

```python
"""Typed errors mapped from OpenRouter HTTP responses.

Callers branch on type rather than status code, so the mapping lives here
once. Spec section 10 defines the operator-facing behaviour for each.
"""

from __future__ import annotations

# Substrings that identify a content-policy refusal rather than a malformed
# request. Both surface as HTTP 400, but they mean very different things to
# the operator, so they get different types.
_MODERATION_MARKERS = ("content policy", "moderation", "safety")


class OpenRouterError(Exception):
    """Base type for every provider-originated failure."""

    #: Whether a request that raised this may still have been billed.
    may_have_charged: bool = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class InvalidRequestError(OpenRouterError):
    """The request was rejected as malformed or unsupported (HTTP 400)."""


class ModerationError(OpenRouterError):
    """The provider refused on content-policy grounds."""


class AuthError(OpenRouterError):
    """The API key is missing, malformed, or unrecognised (HTTP 401)."""


class InsufficientCreditsError(OpenRouterError):
    """The key's credit limit is exhausted (HTTP 402).

    This is the provider-enforced spend guard described in spec section 3.2,
    and must be surfaced distinctly from the local daily cap.
    """


class RateLimitError(OpenRouterError):
    """Too many requests (HTTP 429). Retryable with backoff."""


class ProviderError(OpenRouterError):
    """An upstream failure (HTTP 5xx)."""


class IndeterminateError(OpenRouterError):
    """A request failed after being sent, so its billing state is unknown.

    Never retried automatically: image generation is synchronous and
    non-idempotent, so a retry risks a second charge (spec section 4.4).
    """

    may_have_charged = True


def _message_of(body: dict | None) -> str:
    if not body:
        return "no response body"
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message", "unknown error"))
    if isinstance(error, str):
        return error
    return "unknown error"


def error_from_response(status_code: int, body: dict | None) -> OpenRouterError:
    """Build the appropriate error for an HTTP response."""
    message = _message_of(body)

    if status_code == 400:
        lowered = message.lower()
        if any(marker in lowered for marker in _MODERATION_MARKERS):
            return ModerationError(message, status_code=status_code)
        return InvalidRequestError(message, status_code=status_code)
    if status_code == 401:
        return AuthError(message, status_code=status_code)
    if status_code == 402:
        return InsufficientCreditsError(message, status_code=status_code)
    if status_code == 429:
        return RateLimitError(message, status_code=status_code)
    if status_code >= 500:
        return ProviderError(message, status_code=status_code)
    return OpenRouterError(message, status_code=status_code)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/test_errors.py -v`

Expected: PASS — `10 passed` (the first test is parametrized over six status codes).

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/ tests/orclient/
git commit -m "feat: add typed OpenRouter error hierarchy"
```

---

## Task 4: Catalogue types

**Files:**
- Create: `src/higgshole/orclient/types.py`
- Create: `tests/orclient/test_types.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `TERMINAL_STATUSES: frozenset[str]`, `is_terminal(status: str) -> bool`
  - `VideoModel`, `ImageModel`, `ImageResult`, `VideoJob`, `KeyStatus` — all frozen dataclasses
  - `VideoModel.from_api(payload: dict) -> VideoModel`
  - `ImageModel.from_api(payload: dict) -> ImageModel`
  - `VideoJob.from_api(payload: dict) -> VideoJob`
  - `KeyStatus.from_api(payload: dict) -> KeyStatus`

- [ ] **Step 1: Write the failing tests**

Create `tests/orclient/test_types.py`:

```python
from decimal import Decimal

from higgshole.orclient.types import (
    ImageModel,
    KeyStatus,
    VideoJob,
    VideoModel,
    is_terminal,
)

# Trimmed from a live GET /api/v1/videos/models response.
VIDEO_MODEL_PAYLOAD = {
    "id": "kwaivgi/kling-v3.0-pro",
    "supported_resolutions": ["720p"],
    "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
    "supported_durations": [3, 4, 5, 10],
    "supported_sizes": ["1280x720", "720x1280"],
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": True,
    "pricing_skus": {
        "duration_seconds": "0.112",
        "duration_seconds_with_audio": "0.168",
        "image_to_video_duration_seconds_1080p": "0.112",
    },
    "allowed_passthrough_parameters": ["negative_prompt", "cfg_scale"],
}

IMAGE_MODEL_PAYLOAD = {
    "id": "openai/gpt-image-2",
    "name": "GPT Image 2",
    "supported_parameters": {
        "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]},
        "n": {"type": "range", "min": 1, "max": 10},
        "input_references": {"type": "range", "min": 0, "max": 16},
    },
    "supports_streaming": True,
}


def test_video_model_parses_capabilities():
    model = VideoModel.from_api(VIDEO_MODEL_PAYLOAD)

    assert model.id == "kwaivgi/kling-v3.0-pro"
    assert model.supported_durations == (3, 4, 5, 10)
    assert model.supported_frame_images == ("first_frame", "last_frame")
    assert model.generate_audio is True
    assert model.pricing_skus["duration_seconds_with_audio"] == "0.168"


def test_video_model_tolerates_absent_optional_fields():
    model = VideoModel.from_api({"id": "some/model"})

    assert model.supported_durations == ()
    assert model.supported_frame_images == ()
    assert model.generate_audio is None
    assert model.pricing_skus == {}


def test_video_model_reports_reference_support():
    assert VideoModel.from_api(VIDEO_MODEL_PAYLOAD).accepts_frame_images is True
    # Sora 2 Pro accepts none — spec section 2.7.
    assert VideoModel.from_api({"id": "openai/sora-2-pro"}).accepts_frame_images is False


def test_image_model_extracts_reference_limit():
    model = ImageModel.from_api(IMAGE_MODEL_PAYLOAD)

    assert model.id == "openai/gpt-image-2"
    assert model.max_input_references == 16
    assert model.quality_values == ("auto", "low", "medium", "high")


def test_image_model_without_reference_support_reports_zero():
    model = ImageModel.from_api({"id": "some/model", "supported_parameters": {}})

    assert model.max_input_references == 0
    assert model.quality_values == ()


def test_terminal_status_set_matches_the_specification():
    for status in ("completed", "failed", "cancelled", "expired"):
        assert is_terminal(status) is True
    for status in ("pending", "in_progress"):
        assert is_terminal(status) is False


def test_unknown_status_is_non_terminal_so_polling_continues():
    # Spec section 2.4: treating a live job as terminal loses a paid
    # generation; over-polling is bounded by the wall-clock ceiling.
    assert is_terminal("something_new") is False


def test_video_job_parses_a_completed_response():
    job = VideoJob.from_api(
        {
            "id": "abc123",
            "status": "completed",
            "generation_id": "gen-1",
            "unsigned_urls": ["https://storage.example.com/video.mp4"],
            "usage": {"cost": 0.25, "is_byok": False},
        }
    )

    assert job.id == "abc123"
    assert job.is_terminal is True
    assert job.cost == Decimal("0.25")
    assert job.result_urls == ("https://storage.example.com/video.mp4",)


def test_video_job_with_null_cost_reports_none_not_zero():
    job = VideoJob.from_api(
        {"id": "abc", "status": "completed", "usage": {"cost": None}}
    )

    assert job.cost is None


def test_video_job_without_usage_reports_none():
    job = VideoJob.from_api({"id": "abc", "status": "completed"})

    assert job.cost is None


def test_video_job_surfaces_the_error_string():
    job = VideoJob.from_api(
        {"id": "abc", "status": "failed", "error": "Content policy violation"}
    )

    assert job.is_terminal is True
    assert job.error == "Content policy violation"


def test_key_status_parses_authoritative_budget_figures():
    status = KeyStatus.from_api(
        {
            "data": {
                "limit": 100,
                "limit_remaining": 74.5,
                "limit_reset": "monthly",
                "usage": 25.5,
                "usage_daily": 25.5,
                "is_free_tier": False,
            }
        }
    )

    assert status.limit_remaining == Decimal("74.5")
    assert status.usage_daily == Decimal("25.5")
    assert status.is_free_tier is False


def test_key_status_handles_an_unlimited_key():
    status = KeyStatus.from_api({"data": {"limit": None, "limit_remaining": None}})

    assert status.limit is None
    assert status.limit_remaining is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_types.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.orclient.types'`.

- [ ] **Step 3: Implement the types module**

Create `src/higgshole/orclient/types.py`:

```python
"""Frozen value types parsed from OpenRouter responses.

Every ``from_api`` classmethod tolerates missing optional fields, because
the live catalogue is not uniform across models and the OpenAPI schema
marks much of it optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

#: Statuses after which no further polling should occur (spec section 2.4).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)


def is_terminal(status: str) -> bool:
    """Whether a provider job status ends the polling loop.

    Unrecognised statuses are deliberately treated as NON-terminal. Polling a
    finished job wastes a few requests and self-corrects at the wall-clock
    ceiling; abandoning a live job loses a paid generation irrecoverably.
    """
    return status in TERMINAL_STATUSES


def _decimal_or_none(value: Any) -> Decimal | None:
    """Convert to Decimal, preserving the difference between absent and zero."""
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class VideoModel:
    id: str
    supported_resolutions: tuple[str, ...] = ()
    supported_aspect_ratios: tuple[str, ...] = ()
    supported_durations: tuple[int, ...] = ()
    supported_sizes: tuple[str, ...] = ()
    supported_frame_images: tuple[str, ...] = ()
    generate_audio: bool | None = None
    seed: bool = False
    pricing_skus: Mapping[str, str] = field(default_factory=dict)
    allowed_passthrough_parameters: tuple[str, ...] = ()

    @property
    def accepts_frame_images(self) -> bool:
        return bool(self.supported_frame_images)

    @classmethod
    def from_api(cls, payload: dict) -> VideoModel:
        return cls(
            id=payload["id"],
            supported_resolutions=tuple(payload.get("supported_resolutions") or ()),
            supported_aspect_ratios=tuple(payload.get("supported_aspect_ratios") or ()),
            supported_durations=tuple(payload.get("supported_durations") or ()),
            supported_sizes=tuple(payload.get("supported_sizes") or ()),
            supported_frame_images=tuple(payload.get("supported_frame_images") or ()),
            generate_audio=payload.get("generate_audio"),
            seed=bool(payload.get("seed")),
            pricing_skus=MappingProxyType(dict(payload.get("pricing_skus") or {})),
            allowed_passthrough_parameters=tuple(
                payload.get("allowed_passthrough_parameters") or ()
            ),
        )


@dataclass(frozen=True)
class ImageModel:
    id: str
    name: str = ""
    max_input_references: int = 0
    quality_values: tuple[str, ...] = ()
    max_n: int = 1
    supports_streaming: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> ImageModel:
        params = payload.get("supported_parameters") or {}

        references = params.get("input_references") or {}
        quality = params.get("quality") or {}
        n_param = params.get("n") or {}

        return cls(
            id=payload["id"],
            name=payload.get("name") or "",
            max_input_references=int(references.get("max", 0)),
            quality_values=tuple(quality.get("values") or ()),
            max_n=int(n_param.get("max", 1)),
            supports_streaming=bool(payload.get("supports_streaming")),
        )


@dataclass(frozen=True)
class ImageResult:
    """One generated image, still in memory. Persisting it is store/'s job."""

    data: bytes
    media_type: str
    cost: Decimal | None


@dataclass(frozen=True)
class VideoJob:
    id: str
    status: str
    generation_id: str | None = None
    result_urls: tuple[str, ...] = ()
    cost: Decimal | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @classmethod
    def from_api(cls, payload: dict) -> VideoJob:
        usage = payload.get("usage") or {}
        return cls(
            id=payload["id"],
            status=payload["status"],
            generation_id=payload.get("generation_id"),
            result_urls=tuple(payload.get("unsigned_urls") or ()),
            cost=_decimal_or_none(usage.get("cost")),
            error=payload.get("error"),
        )


@dataclass(frozen=True)
class KeyStatus:
    """Authoritative budget figures from GET /api/v1/key (spec section 3.2)."""

    limit: Decimal | None = None
    limit_remaining: Decimal | None = None
    limit_reset: str | None = None
    usage: Decimal | None = None
    usage_daily: Decimal | None = None
    is_free_tier: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> KeyStatus:
        data = payload.get("data") or payload
        return cls(
            limit=_decimal_or_none(data.get("limit")),
            limit_remaining=_decimal_or_none(data.get("limit_remaining")),
            limit_reset=data.get("limit_reset"),
            usage=_decimal_or_none(data.get("usage")),
            usage_daily=_decimal_or_none(data.get("usage_daily")),
            is_free_tier=bool(data.get("is_free_tier")),
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/test_types.py -v`

Expected: PASS — `13 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/types.py tests/orclient/test_types.py
git commit -m "feat: add typed value objects for models, jobs and key status"
```

---

## Task 5: Client construction and catalogue fetch

**Files:**
- Create: `src/higgshole/orclient/client.py`
- Create: `tests/conftest.py`
- Create: `tests/orclient/test_catalog_fetch.py`

**Interfaces:**
- Consumes: `errors.error_from_response`, `types.VideoModel`, `types.ImageModel`.
- Produces: `OpenRouterClient(api_key: str, *, base_url: str = "https://openrouter.ai/api/v1", timeout: float = 30.0)`, an async context manager, with:
  - `async list_video_models() -> tuple[VideoModel, ...]`
  - `async list_image_models() -> tuple[ImageModel, ...]`
  - `async get_image_model_pricing(model_id: str) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Create `tests/conftest.py`:

```python
import pytest

from higgshole.orclient.client import OpenRouterClient

BASE_URL = "https://openrouter.ai/api/v1"


@pytest.fixture
async def client():
    """A client pointed at the real base URL, with all traffic intercepted."""
    async with OpenRouterClient(api_key="sk-or-v1-test", base_url=BASE_URL) as c:
        yield c
```

Create `tests/orclient/test_catalog_fetch.py`:

```python
import httpx
import pytest
import respx

from higgshole.orclient.errors import AuthError, RateLimitError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_list_video_models_parses_the_catalogue(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "google/veo-3.1",
                        "supported_durations": [4, 6, 8],
                        "supported_frame_images": ["first_frame", "last_frame"],
                        "pricing_skus": {"duration_seconds_with_audio": "0.40"},
                    },
                    {"id": "openai/sora-2-pro", "supported_durations": [4, 8]},
                ]
            },
        )
    )

    models = await client.list_video_models()

    assert [m.id for m in models] == ["google/veo-3.1", "openai/sora-2-pro"]
    assert models[0].accepts_frame_images is True
    assert models[1].accepts_frame_images is False


@respx.mock
async def test_catalogue_accepts_a_bare_list_response(client):
    # The endpoint has been observed returning a bare array rather than
    # {"data": [...]}, so both shapes must parse.
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(200, json=[{"id": "a/b"}])
    )

    models = await client.list_video_models()

    assert [m.id for m in models] == ["a/b"]


@respx.mock
async def test_list_image_models_parses_reference_limits(client):
    respx.get(f"{BASE_URL}/images/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-image-2",
                        "supported_parameters": {
                            "input_references": {"type": "range", "min": 0, "max": 16}
                        },
                    }
                ]
            },
        )
    )

    models = await client.list_image_models()

    assert models[0].max_input_references == 16


@respx.mock
async def test_image_pricing_is_fetched_per_model(client):
    respx.get(f"{BASE_URL}/images/models/openai/gpt-image-2/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {
                            "provider_name": "OpenAI",
                            "pricing": [
                                {
                                    "billable": "output_image",
                                    "unit": "token",
                                    "cost_usd": 3e-05,
                                }
                            ],
                        }
                    ]
                }
            },
        )
    )

    pricing = await client.get_image_model_pricing("openai/gpt-image-2")

    assert pricing[0]["unit"] == "token"


@respx.mock
async def test_the_api_key_is_sent_as_a_bearer_token(client):
    route = respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    await client.list_video_models()

    assert route.calls.last.request.headers["authorization"] == "Bearer sk-or-v1-test"


@respx.mock
async def test_a_401_raises_auth_error(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "User not found.", "code": 401}}
        )
    )

    with pytest.raises(AuthError, match="User not found"):
        await client.list_video_models()


@respx.mock
async def test_a_429_raises_rate_limit_error(client):
    respx.get(f"{BASE_URL}/videos/models").mock(
        return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
    )

    with pytest.raises(RateLimitError):
        await client.list_video_models()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_catalog_fetch.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.orclient.client'`.

- [ ] **Step 3: Implement the client and catalogue methods**

Create `src/higgshole/orclient/client.py`:

```python
"""HTTP client for the OpenRouter image and video generation APIs.

This module performs no filesystem or database access whatsoever. It returns
bytes and value objects; persisting them belongs to store/. That boundary is
what lets the entire provider integration be tested offline and for free.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from .errors import error_from_response
from .types import ImageModel, VideoModel

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals -------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = None
        raise error_from_response(response.status_code, body)

    async def _get_json(self, path: str) -> Any:
        response = await self._client.get(path)
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _entries(payload: Any) -> list[dict]:
        """Normalise ``{"data": [...]}`` and bare-array response shapes."""
        if isinstance(payload, dict):
            return list(payload.get("data") or [])
        return list(payload or [])

    # -- catalogue -------------------------------------------------------

    async def list_video_models(self) -> tuple[VideoModel, ...]:
        payload = await self._get_json("/videos/models")
        return tuple(VideoModel.from_api(entry) for entry in self._entries(payload))

    async def list_image_models(self) -> tuple[ImageModel, ...]:
        payload = await self._get_json("/images/models")
        return tuple(ImageModel.from_api(entry) for entry in self._entries(payload))

    async def get_image_model_pricing(self, model_id: str) -> list[dict]:
        """Fetch a single image model's pricing line items.

        Image pricing is not present in the catalogue listing; it requires one
        request per model, which is why the caller caches it (spec section 4.2).
        """
        payload = await self._get_json(f"/images/models/{model_id}/endpoints")
        data = payload.get("data") if isinstance(payload, dict) else None
        endpoints = (data or {}).get("endpoints") or []
        if not endpoints:
            return []
        return list(endpoints[0].get("pricing") or [])
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/test_catalog_fetch.py -v`

Expected: PASS — `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/client.py tests/conftest.py tests/orclient/test_catalog_fetch.py
git commit -m "feat: add OpenRouter client with model catalogue fetching"
```

---

## Task 6: Image generation

**Files:**
- Modify: `src/higgshole/orclient/client.py` (append methods to `OpenRouterClient`)
- Create: `tests/orclient/test_images.py`

**Interfaces:**
- Consumes: `types.ImageResult`.
- Produces: `async generate_image(*, model: str, prompt: str, input_references: Sequence[str] = (), **params) -> ImageResult`. `input_references` takes bare URL-or-data-URI strings; the wire envelope is built internally.

- [ ] **Step 1: Write the failing tests**

Create `tests/orclient/test_images.py`:

```python
import base64
import json

import httpx
import pytest
import respx

from higgshole.orclient.errors import IndeterminateError, ModerationError

BASE_URL = "https://openrouter.ai/api/v1"

PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


def _ok(cost=0.04):
    usage = {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10}
    if cost is not None:
        usage["cost"] = cost
    return httpx.Response(
        200,
        json={
            "created": 1748372400,
            "data": [{"b64_json": PIXEL, "media_type": "image/png"}],
            "usage": usage,
        },
    )


@respx.mock
async def test_generate_image_decodes_the_payload(client):
    respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    result = await client.generate_image(model="a/b", prompt="a cat")

    assert result.data.startswith(b"\x89PNG")
    assert result.media_type == "image/png"
    assert str(result.cost) == "0.04"


@respx.mock
async def test_missing_cost_is_none_rather_than_zero(client):
    # Spec section 3.4: recording zero would let the daily cap never trip.
    respx.post(f"{BASE_URL}/images").mock(return_value=_ok(cost=None))

    result = await client.generate_image(model="a/b", prompt="a cat")

    assert result.cost is None


@respx.mock
async def test_optional_parameters_are_forwarded(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(
        model="a/b", prompt="a cat", aspect_ratio="16:9", quality="high", seed=7
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["aspect_ratio"] == "16:9"
    assert sent["quality"] == "high"
    assert sent["seed"] == 7


@respx.mock
async def test_unset_parameters_are_omitted_entirely(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(model="a/b", prompt="a cat")

    sent = json.loads(route.calls.last.request.read())
    assert set(sent) == {"model", "prompt"}


@respx.mock
async def test_input_references_are_wrapped_in_the_wire_envelope(client):
    route = respx.post(f"{BASE_URL}/images").mock(return_value=_ok())

    await client.generate_image(
        model="a/b",
        prompt="make it watercolour",
        input_references=["https://example.com/p.jpg", "data:image/png;base64,AAAA"],
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["input_references"] == [
        {"type": "image_url", "image_url": {"url": "https://example.com/p.jpg"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


@respx.mock
async def test_a_moderation_refusal_raises_its_own_type(client):
    respx.post(f"{BASE_URL}/images").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Content policy violation"}}
        )
    )

    with pytest.raises(ModerationError):
        await client.generate_image(model="a/b", prompt="nope")


@respx.mock
async def test_a_connection_failure_after_submit_is_indeterminate(client):
    # Image generation is synchronous and non-idempotent, so the caller must
    # never silently retry — the charge may already have happened.
    respx.post(f"{BASE_URL}/images").mock(side_effect=httpx.ConnectError("reset"))

    with pytest.raises(IndeterminateError) as caught:
        await client.generate_image(model="a/b", prompt="a cat")

    assert caught.value.may_have_charged is True


@respx.mock
async def test_an_empty_data_array_is_a_provider_error(client):
    respx.post(f"{BASE_URL}/images").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )

    with pytest.raises(Exception, match="no image data"):
        await client.generate_image(model="a/b", prompt="a cat")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_images.py -v`

Expected: FAIL — `AttributeError: 'OpenRouterClient' object has no attribute 'generate_image'`.

- [ ] **Step 3: Implement image generation**

In `src/higgshole/orclient/client.py`, extend the imports:

```python
from collections.abc import Sequence
```

and

```python
from .errors import IndeterminateError, ProviderError, error_from_response
from .types import ImageModel, ImageResult, VideoModel
```

Add a module-level helper above the class:

```python
def _image_reference(url: str) -> dict:
    """Wrap a URL or data URI in OpenRouter's ContentPartImage envelope."""
    return {"type": "image_url", "image_url": {"url": url}}


def _without_nones(params: dict[str, Any]) -> dict[str, Any]:
    """Drop unset parameters so the provider applies its own defaults."""
    return {key: value for key, value in params.items() if value is not None}
```

Add these methods to `OpenRouterClient`:

```python
    async def _post_json(self, path: str, payload: dict) -> Any:
        """POST a body, converting post-send transport failures to
        IndeterminateError so callers never blindly retry a possible charge.
        """
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise IndeterminateError(f"timed out after sending: {exc}") from exc
        except httpx.TransportError as exc:
            raise IndeterminateError(f"connection failed after sending: {exc}") from exc

        self._raise_for_status(response)
        return response.json()

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        input_references: Sequence[str] = (),
        **params: Any,
    ) -> ImageResult:
        """Generate one image synchronously.

        ``params`` accepts any of the documented optional fields (aspect_ratio,
        resolution, size, quality, output_format, background, seed, ...).
        Unset values are omitted rather than sent as null.
        """
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(_without_nones(params))
        if input_references:
            body["input_references"] = [_image_reference(u) for u in input_references]

        payload = await self._post_json("/images", body)

        entries = payload.get("data") or []
        if not entries:
            raise ProviderError("response contained no image data")

        first = entries[0]
        usage = payload.get("usage") or {}
        cost = usage.get("cost")

        return ImageResult(
            data=base64.b64decode(first["b64_json"]),
            media_type=first.get("media_type") or "image/png",
            cost=None if cost is None else Decimal(str(cost)),
        )
```

Add the two remaining imports at the top of the module:

```python
import base64
from decimal import Decimal
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/test_images.py -v`

Expected: PASS — `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/client.py tests/orclient/test_images.py
git commit -m "feat: add synchronous image generation"
```

---

## Task 7: Video submit, poll and download

**Files:**
- Modify: `src/higgshole/orclient/client.py`
- Modify: `src/higgshole/orclient/__init__.py`
- Create: `tests/orclient/test_videos.py`

**Interfaces:**
- Consumes: `types.VideoJob`.
- Produces:
  - `async submit_video(*, model: str, prompt: str, frame_images: Sequence[tuple[str, str]] = (), input_references: Sequence[str] = (), **params) -> VideoJob` — `frame_images` items are `(url, frame_type)` pairs.
  - `async get_video_job(job_id: str) -> VideoJob`
  - `async download_video(job_id: str, *, index: int = 0) -> bytes`
- `orclient/__init__.py` re-exports `OpenRouterClient`, every error type, and every value type.

- [ ] **Step 1: Write the failing tests**

Create `tests/orclient/test_videos.py`:

```python
import json

import httpx
import pytest
import respx

from higgshole.orclient.errors import ProviderError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_submit_returns_the_job_id_immediately(client):
    respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "abc123",
                "status": "pending",
                "polling_url": f"{BASE_URL}/videos/abc123",
            },
        )
    )

    job = await client.submit_video(model="google/veo-3.1", prompt="a beach")

    assert job.id == "abc123"
    assert job.is_terminal is False


@respx.mock
async def test_frame_images_carry_their_frame_type(client):
    route = respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(202, json={"id": "a", "status": "pending"})
    )

    await client.submit_video(
        model="kwaivgi/kling-v3.0-pro",
        prompt="pan across",
        frame_images=[
            ("https://example.com/first.jpg", "first_frame"),
            ("data:image/png;base64,AAAA", "last_frame"),
        ],
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/first.jpg"},
            "frame_type": "first_frame",
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "frame_type": "last_frame",
        },
    ]


@respx.mock
async def test_a_callback_url_is_never_sent(client):
    # Spec section 2.6: webhooks are out of scope; polling only.
    route = respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(202, json={"id": "a", "status": "pending"})
    )

    await client.submit_video(model="a/b", prompt="x", duration=8)

    sent = json.loads(route.calls.last.request.read())
    assert "callback_url" not in sent
    assert sent["duration"] == 8


@respx.mock
@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_non_terminal_statuses_keep_polling(client, status):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": status})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is False


@respx.mock
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "expired"])
async def test_all_four_terminal_statuses_end_polling(client, status):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": status})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is True


@respx.mock
async def test_an_unrecognised_status_does_not_end_polling(client):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "status": "reticulating"})
    )

    job = await client.get_video_job("abc")

    assert job.is_terminal is False
    assert job.status == "reticulating"


@respx.mock
async def test_a_failed_job_surfaces_its_error_string(client):
    respx.get(f"{BASE_URL}/videos/abc").mock(
        return_value=httpx.Response(
            200,
            json={"id": "abc", "status": "failed", "error": "Content policy violation"},
        )
    )

    job = await client.get_video_job("abc")

    assert job.error == "Content policy violation"


@respx.mock
async def test_download_returns_raw_bytes(client):
    respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(200, content=b"\x00\x00\x00 ftypmp42")
    )

    data = await client.download_video("abc")

    assert data.startswith(b"\x00\x00\x00 ftyp")


@respx.mock
async def test_download_passes_the_output_index(client):
    route = respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(200, content=b"x")
    )

    await client.download_video("abc", index=2)

    assert route.calls.last.request.url.params["index"] == "2"


@respx.mock
async def test_a_502_on_download_is_a_provider_error(client):
    # OpenRouter proxies from the upstream provider at download time, so a 502
    # here may mean the provider's retention window has lapsed.
    respx.get(f"{BASE_URL}/videos/abc/content").mock(
        return_value=httpx.Response(502, json={"error": {"message": "upstream"}})
    )

    with pytest.raises(ProviderError):
        await client.download_video("abc")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_videos.py -v`

Expected: FAIL — `AttributeError: 'OpenRouterClient' object has no attribute 'submit_video'`.

- [ ] **Step 3: Implement the video methods**

Add to the imports in `client.py`:

```python
from .types import ImageModel, ImageResult, VideoJob, VideoModel
```

Add a module-level helper beside `_image_reference`:

```python
def _frame_image(url: str, frame_type: str) -> dict:
    """A ContentPartImage plus the required frame_type discriminator."""
    return {**_image_reference(url), "frame_type": frame_type}
```

Add these methods to `OpenRouterClient`:

```python
    async def submit_video(
        self,
        *,
        model: str,
        prompt: str,
        frame_images: Sequence[tuple[str, str]] = (),
        input_references: Sequence[str] = (),
        **params: Any,
    ) -> VideoJob:
        """Submit an asynchronous video job and return immediately.

        ``frame_images`` items are ``(url, frame_type)`` pairs where frame_type
        is "first_frame" or "last_frame". If both frame_images and
        input_references are supplied the provider honours frame_images and
        ignores the rest, so callers should send only one.

        No callback_url is ever sent: webhooks are out of scope (spec 2.6).
        """
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        body.update(_without_nones(params))
        body.pop("callback_url", None)

        if frame_images:
            body["frame_images"] = [_frame_image(u, t) for u, t in frame_images]
        elif input_references:
            body["input_references"] = [_image_reference(u) for u in input_references]

        payload = await self._post_json("/videos", body)
        return VideoJob.from_api(payload)

    async def get_video_job(self, job_id: str) -> VideoJob:
        """Poll a job's current state. Safe to retry — an idempotent GET."""
        payload = await self._get_json(f"/videos/{job_id}")
        return VideoJob.from_api(payload)

    async def download_video(self, job_id: str, *, index: int = 0) -> bytes:
        """Fetch the rendered video.

        Must be called as soon as the job reports completed: OpenRouter streams
        from the upstream provider rather than storing the result, and no
        retention window is published (spec section 2.5).
        """
        response = await self._client.get(
            f"/videos/{job_id}/content", params={"index": index}
        )
        self._raise_for_status(response)
        return response.content
```

Now populate `src/higgshole/orclient/__init__.py`:

```python
"""OpenRouter API client.

Performs no filesystem or database access, so the whole provider integration
is testable against recorded fixtures with no network and no spend.
"""

from .client import DEFAULT_BASE_URL, OpenRouterClient
from .errors import (
    AuthError,
    IndeterminateError,
    InsufficientCreditsError,
    InvalidRequestError,
    ModerationError,
    OpenRouterError,
    ProviderError,
    RateLimitError,
)
from .types import (
    TERMINAL_STATUSES,
    ImageModel,
    ImageResult,
    KeyStatus,
    VideoJob,
    VideoModel,
    is_terminal,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "TERMINAL_STATUSES",
    "AuthError",
    "ImageModel",
    "ImageResult",
    "IndeterminateError",
    "InsufficientCreditsError",
    "InvalidRequestError",
    "KeyStatus",
    "ModerationError",
    "OpenRouterClient",
    "OpenRouterError",
    "ProviderError",
    "RateLimitError",
    "VideoJob",
    "VideoModel",
    "is_terminal",
]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/ -v`

Expected: PASS — every test in the package, `52 passed` (errors 10, types 13, catalogue 7, images 8, videos 14).

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/ tests/orclient/test_videos.py
git commit -m "feat: add asynchronous video submit, poll and download"
```

---

## Task 8: Key status and budget figures

**Files:**
- Modify: `src/higgshole/orclient/client.py`
- Create: `tests/orclient/test_key.py`

**Interfaces:**
- Consumes: `types.KeyStatus`, `errors.AuthError`.
- Produces:
  - `async get_key_status() -> KeyStatus`
  - `async validate_key() -> bool` — `True` if the key authenticates, `False` on `AuthError`, other errors propagate.
  - `looks_like_openrouter_key(candidate: str) -> bool` — module-level.

- [ ] **Step 1: Write the failing tests**

Create `tests/orclient/test_key.py`:

```python
import httpx
import pytest
import respx

from higgshole.orclient.client import looks_like_openrouter_key
from higgshole.orclient.errors import ProviderError

BASE_URL = "https://openrouter.ai/api/v1"


@respx.mock
async def test_key_status_returns_authoritative_budget_figures(client):
    # Spec section 3.2: this call is free and is the source of truth for
    # remaining budget, in preference to the local ledger.
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "limit": 100,
                    "limit_remaining": 74.5,
                    "limit_reset": "monthly",
                    "usage": 25.5,
                    "usage_daily": 25.5,
                    "is_free_tier": False,
                }
            },
        )
    )

    status = await client.get_key_status()

    assert str(status.limit_remaining) == "74.5"
    assert str(status.usage_daily) == "25.5"


@respx.mock
async def test_validate_key_is_true_for_a_working_key(client):
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(200, json={"data": {"limit": None}})
    )

    assert await client.validate_key() is True


@respx.mock
async def test_validate_key_is_false_for_a_rejected_key(client):
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(401, json={"error": {"message": "User not found."}})
    )

    assert await client.validate_key() is False


@respx.mock
async def test_validate_key_propagates_non_auth_failures(client):
    # A provider outage must not be reported to the operator as a bad key.
    respx.get(f"{BASE_URL}/key").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )

    with pytest.raises(ProviderError):
        await client.validate_key()


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("sk-or-v1-abcdef0123456789", True),
        ("sk-proj-abcdef0123456789", False),  # an OpenAI key
        ("sk-or-abc", False),  # missing the v1 segment
        ("sk-or-v1-", False),  # prefix with no payload
        ("", False),
        ("   ", False),
    ],
)
def test_key_shape_is_checked_before_submission(candidate, expected):
    # The server's messages do not clearly distinguish a foreign key from an
    # absent one, so the shape check happens locally (spec section 7).
    assert looks_like_openrouter_key(candidate) is expected
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/orclient/test_key.py -v`

Expected: FAIL — `ImportError: cannot import name 'looks_like_openrouter_key'`.

- [ ] **Step 3: Implement key handling**

Add to the imports in `client.py`:

```python
from .errors import (
    AuthError,
    IndeterminateError,
    ProviderError,
    error_from_response,
)
from .types import ImageModel, ImageResult, KeyStatus, VideoJob, VideoModel
```

Add a module-level constant and function:

```python
#: OpenRouter keys begin with this prefix followed by a non-empty payload.
_KEY_PREFIX = "sk-or-v1-"


def looks_like_openrouter_key(candidate: str) -> bool:
    """Whether a pasted string has the shape of an OpenRouter key.

    Checked client-side because the server's 401 messages are misleading: a
    key with a foreign prefix yields "Missing Authentication header", the same
    text an empty field produces.
    """
    candidate = candidate.strip()
    return candidate.startswith(_KEY_PREFIX) and len(candidate) > len(_KEY_PREFIX)
```

Add these methods to `OpenRouterClient`:

```python
    async def get_key_status(self) -> KeyStatus:
        """Fetch the key's authoritative limit and usage figures.

        Free to call, and the source of truth for remaining budget.
        """
        return KeyStatus.from_api(await self._get_json("/key"))

    async def validate_key(self) -> bool:
        """Whether the configured key authenticates. Costs nothing.

        Returns False only for an authentication failure; every other error
        propagates, so a provider outage is never reported as a bad key.
        """
        try:
            await self.get_key_status()
        except AuthError:
            return False
        return True
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/orclient/test_key.py -v`

Expected: PASS — `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/orclient/client.py tests/orclient/test_key.py
git commit -m "feat: add key validation and authoritative budget figures"
```

---

## Task 9: Parameter validation and the advisory/hard precedence rule

**Files:**
- Create: `src/higgshole/catalog/__init__.py`
- Create: `src/higgshole/catalog/validation.py`
- Create: `tests/catalog/__init__.py`
- Create: `tests/catalog/test_validation.py`

**Interfaces:**
- Consumes: `orclient.types.VideoModel`, `orclient.types.ImageModel`.
- Produces:
  - `Severity` — `StrEnum` with `HARD` and `ADVISORY`
  - `ValidationIssue(parameter: str, value: str, severity: Severity, message: str)`
  - `validate_video_request(model, *, resolution=None, aspect_ratio=None, duration=None, frame_types=()) -> list[ValidationIssue]`
  - `validate_image_request(model, *, n=1, quality=None, reference_count=0, daily_cap_set=False) -> list[ValidationIssue]`
  - `has_hard_failure(issues) -> bool`

This implements spec §2.7's precedence rule: a value absent from the declared
capability list but present in `pricing_skus` is **advisory**; a value absent
from both is **hard invalid**.

- [ ] **Step 1: Write the failing tests**

Create `tests/catalog/__init__.py` (empty) and `tests/catalog/test_validation.py`:

```python
from higgshole.catalog.validation import (
    Severity,
    has_hard_failure,
    validate_image_request,
    validate_video_request,
)
from higgshole.orclient.types import ImageModel, VideoModel

# Kling declares only 720p but prices 480p and 1080p — spec section 2.7.
KLING = VideoModel.from_api(
    {
        "id": "kwaivgi/kling-v3.0-pro",
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_durations": [3, 5, 10],
        "supported_frame_images": ["first_frame", "last_frame"],
        "pricing_skus": {
            "text_to_video_duration_seconds_480p": "0.112",
            "image_to_video_duration_seconds_1080p": "0.112",
        },
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

HAILUO = VideoModel.from_api(
    {
        "id": "minimax/hailuo-2.3",
        "supported_resolutions": ["1080p"],
        "supported_durations": [6, 10],
        "supported_frame_images": ["first_frame"],
    }
)

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
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
        "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 1}},
    }
)


def test_a_declared_value_produces_no_issue():
    assert validate_video_request(KLING, resolution="720p", duration=5) == []


def test_an_undeclared_but_priced_value_is_advisory():
    issues = validate_video_request(KLING, resolution="1080p")

    assert len(issues) == 1
    assert issues[0].severity is Severity.ADVISORY
    assert has_hard_failure(issues) is False


def test_a_value_absent_from_both_lists_is_a_hard_failure():
    issues = validate_video_request(KLING, resolution="8K")

    assert issues[0].severity is Severity.HARD
    assert has_hard_failure(issues) is True


def test_an_unsupported_duration_is_a_hard_failure():
    issues = validate_video_request(KLING, duration=7)

    assert has_hard_failure(issues) is True
    assert "7" in issues[0].value


def test_a_model_accepting_no_frames_rejects_any_reference():
    issues = validate_video_request(SORA, frame_types=["first_frame"])

    assert has_hard_failure(issues) is True
    assert "sora" in issues[0].message.lower() or "no reference" in issues[0].message.lower()


def test_a_first_frame_only_model_rejects_a_last_frame():
    issues = validate_video_request(HAILUO, frame_types=["last_frame"])

    assert has_hard_failure(issues) is True


def test_a_first_frame_only_model_accepts_a_first_frame():
    assert validate_video_request(HAILUO, frame_types=["first_frame"]) == []


def test_an_unsupported_aspect_ratio_is_a_hard_failure():
    issues = validate_video_request(KLING, aspect_ratio="21:9")

    assert has_hard_failure(issues) is True


def test_multiple_problems_are_all_reported():
    issues = validate_video_request(KLING, resolution="8K", duration=99)

    assert len(issues) == 2


def test_too_many_image_references_is_a_hard_failure():
    issues = validate_image_request(RECRAFT, reference_count=3)

    assert has_hard_failure(issues) is True
    assert "1" in issues[0].message


def test_reference_count_within_the_limit_is_accepted():
    assert validate_image_request(GPT_IMAGE, reference_count=5) == []


def test_batch_generation_is_rejected():
    # Spec section 5.5: n is fixed at 1.
    issues = validate_image_request(GPT_IMAGE, n=4)

    assert has_hard_failure(issues) is True


def test_auto_quality_is_rejected_when_a_daily_cap_is_configured():
    # Spec section 3.5: auto quality on token-billed models is unbounded.
    issues = validate_image_request(GPT_IMAGE, quality="auto", daily_cap_set=True)

    assert has_hard_failure(issues) is True


def test_auto_quality_is_permitted_with_no_cap_configured():
    assert validate_image_request(GPT_IMAGE, quality="auto", daily_cap_set=False) == []


def test_an_unsupported_quality_value_is_a_hard_failure():
    issues = validate_image_request(GPT_IMAGE, quality="ultra")

    assert has_hard_failure(issues) is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/catalog/test_validation.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.catalog'`.

- [ ] **Step 3: Implement validation**

Create `src/higgshole/catalog/validation.py`:

```python
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
```

Create `src/higgshole/catalog/__init__.py`:

```python
"""Model capability catalogue and request validation."""

from .validation import (
    Severity,
    ValidationIssue,
    has_hard_failure,
    validate_image_request,
    validate_video_request,
)

__all__ = [
    "Severity",
    "ValidationIssue",
    "has_hard_failure",
    "validate_image_request",
    "validate_video_request",
]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/catalog/test_validation.py -v`

Expected: PASS — `15 passed`.

- [ ] **Step 5: Run the whole suite and lint, then commit**

Run: `uv run pytest -q && uv run ruff check .`

Expected: all tests pass, `All checks passed!`

```bash
git add src/higgshole/catalog/ tests/catalog/
git commit -m "feat: add capability validation with advisory and hard severities"
```

---

## Task 10: Offline guarantee and CI

**Files:**
- Modify: `tests/conftest.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: everything above.
- Produces: an autouse fixture that fails any test attempting real network I/O, plus CI running the suite on every push.

This task exists because the "no test costs money" guarantee is currently a
convention. Here it becomes enforced.

- [ ] **Step 1: Write the failing test**

Append to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _forbid_real_network(request, monkeypatch):
    """Fail any test that attempts a real network connection.

    Tests must intercept HTTP with respx. A real request would be slow,
    flaky, and — against a generation API — billable.
    """
    if request.node.get_closest_marker("live"):
        return

    import socket

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "This test attempted a real network connection. Use respx to "
            "intercept it, or mark the test with @pytest.mark.live."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
```

Replace `tests/test_smoke.py` entirely — imports must stay at the top of the
file or `ruff`'s E402 rule will fail the lint step:

```python
import httpx
import pytest


def test_package_importable():
    import higgshole

    assert higgshole.__version__ == "0.1.0"


async def test_real_network_access_is_blocked_in_tests():
    with pytest.raises(RuntimeError, match="real network connection"):
        async with httpx.AsyncClient() as client:
            await client.get("https://example.com")
```

- [ ] **Step 2: Run to verify the guard is absent**

Run: `uv run pytest tests/test_smoke.py -v`

Expected: FAIL — the request either succeeds or raises a connection error, not `RuntimeError`.

- [ ] **Step 3: Register the marker**

In `pyproject.toml`, extend the pytest section:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "live: performs a real, billable API call; deselected unless HIGGSHOLE_LIVE_TESTS is set",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q`

Expected: PASS — the full suite, with the network guard active.

> If tests in `tests/orclient/` now fail, respx is being bypassed somewhere. respx intercepts at the httpx transport layer and never opens a socket, so a genuine failure here indicates a real request the fixtures did not cover — which is exactly what this guard is for.

- [ ] **Step 5: Add CI and commit**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --extra dev

      - name: Lint
        run: uv run ruff check .

      - name: Test
        run: uv run pytest -q
```

```bash
git add tests/conftest.py tests/test_smoke.py pyproject.toml .github/workflows/ci.yml
git commit -m "test: forbid real network access in tests and add CI"
```

---

## Definition of done for Plan 1

- [ ] `uv run pytest -q` passes with no network access
- [ ] `uv run ruff check .` is clean
- [ ] `orclient/` imports nothing from `store/`, opens no files, and opens no database connection
- [ ] All six job statuses are covered by tests, plus an unrecognised one
- [ ] No test can make a billable call
- [ ] No committed file contains a personal name, employer name, machine-specific path, or API key
- [ ] CI passes on GitHub

## Deferred to later plans

| Item | Plan |
|---|---|
| Catalogue persistence and TTL refresh (spec §4.2) | 2 — needs `store/` |
| Cost estimation from `pricing_skus` (spec §3.1–3.3) | 2 — belongs with the ledger |
| Retry and backoff policy (spec §4.4) | 3 — belongs with the job engine |
| Reference transport resolution (spec §12.1) | 3 — needs a live key |
