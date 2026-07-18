# HiggsHole Plan 4 — Web Interface

> **How to execute this plan:** work through it strictly task by task, in order.
> Each task is self-contained and ends with a passing test suite and a commit,
> so it is a natural review checkpoint — do not start the next task until the
> current one is green. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Every task follows the same cycle: write a failing test, run it to confirm it
> fails for the reason you expect, write the minimal implementation, confirm it
> passes, commit. Do not write implementation before its test.

**Goal:** Build the FastAPI application — the REST API that both the browser and the MCP server consume, the five HTMX screens, live job status over SSE, and range-capable media serving that is structurally immune to response-rewriting middleware.

**Architecture:** One FastAPI application owns the REST API, the HTMX pages and the SSE stream; every request handler reads a single `AppState` object assembled at startup. Media bytes are served by a *separate* Starlette application that is dispatched **ahead of the parent's middleware stack**, so no middleware — present or future — can rewrite a 206 response. All generation controls are rendered from the cached capability catalogue, never from hardcoded option lists.

**Tech Stack:** Python 3.12+, `uv`, FastAPI, Starlette, Jinja2, HTMX (vendored, no CDN), `uvicorn`, `python-multipart`, pytest, `starlette.testclient`.

**Source specification:** docs/specs/2026-07-18-higgshole-design.md

**Depends on:** Plans 1, 2, 3

## Global Constraints

- **Python 3.12+**, `uv` for dependencies, pytest with `asyncio_mode = "auto"`.
- **Public repository.** No committed file may contain a personal name, an employer name, a machine-specific absolute path, or an API key.
- **No test may make a real network request or cost money.** The autouse `_forbid_real_network` fixture from Plan 1 Task 10 is inherited. `tests/web/` gets an `__init__.py`; it must **not** define a second `conftest.py` that overrides the guard — shared fixtures go into the existing `tests/conftest.py`.
- **Never fabricate a cost.** Every monetary field crossing the HTTP boundary is a `Decimal` rendered as a **string**, or `null`. Never a JSON number, never `0` for unknown (spec §3.4).
- **Terminal job statuses are exactly** `completed`, `failed`, `cancelled`, `expired`. Anything else is non-terminal — keep polling (spec §2.4).
- **Path containment is checked on every media read** via `MediaPaths.resolve_within_root`. There is no unguarded read path (spec §7).
- **API keys are write-only through the UI.** Only `mask_key` output ever leaves the process (spec §7).
- **`GZipMiddleware` is never added anywhere**, and media dispatch happens before the middleware stack so that adding it later still cannot corrupt a 206 (spec §6.3).
- **Exactly one uvicorn worker.** The reservation lock and poller registry are process-local (spec §9).
- **No external hosts.** HTMX and CSS are vendored under `web/static/`; the UI must work on an offline LAN.
- Commit after every task. Conventional commit prefixes (`feat:`, `test:`, `chore:`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/higgshole/web/__init__.py` | Package marker |
| `src/higgshole/web/sse.py` | `EventBus`, the `/events/jobs` stream (re-exports Plan 3's `JobEvent`) |
| `src/higgshole/web/media.py` | Mounted media sub-application; range serving; containment |
| `src/higgshole/web/app.py` | Application factory, `AppState`, lifespan, console entrypoint |
| `src/higgshole/web/api.py` | The REST surface consumed by the browser and by Plan 5 |
| `src/higgshole/web/pages.py` | HTMX screens and partials |
| `src/higgshole/web/templates/` | Jinja2 templates for the five screens and four partials |
| `src/higgshole/web/static/app.css` | Vendored stylesheet |
| `src/higgshole/web/static/vendor/htmx.min.js` | Vendored HTMX, no CDN |
| `tests/web/__init__.py` | Test package marker |
| `tests/web/fakes.py` | Fake catalogue, fake runners, `build_test_state` |
| `tests/web/test_sse.py` | Event bus and stream framing |
| `tests/web/test_media.py` | Range requests, suffix ranges, 416, traversal |
| `tests/web/test_app.py` | Factory, lifespan, single-worker entrypoint |
| `tests/web/test_media_middleware.py` | The 206 / `Content-Encoding` regression |
| `tests/web/test_api_core.py` | Models, projects, budget, masking helpers |
| `tests/web/test_api_generate.py` | Estimate, generate, jobs |
| `tests/web/test_api_library.py` | Uploads, media browse/delete, settings, rescan |
| `tests/web/test_pages.py` | The five screens |
| `tests/web/test_partials.py` | Capability-derived controls |
| `tests/web/test_integration.py` | The assembled application |

---

## Task 1: The event bus and SSE framing

**Files:**
- Modify: `pyproject.toml`
- Create: `src/higgshole/web/__init__.py`
- Create: `src/higgshole/web/sse.py`
- Create: `tests/web/__init__.py`
- Test: `tests/web/test_sse.py`

**Interfaces:**
- Consumes: `higgshole.store.db.GenerationKind`, `GenerationState`, `ErrorReason`, `utc_now_iso` (Plan 2); `higgshole.jobs.events.JobEvent` (Plan 3).
- Produces:
  - A re-export of Plan 3's `JobEvent` — **not** a second definition of it. The runners construct `jobs.events.JobEvent`, so a duplicate frozen dataclass here would only be duck-type compatible and would make every `isinstance`/`Protocol` check lie.
  - `EventBus(*, max_queue: int = 100)` with `publish(event: JobEvent) -> None`, `subscribe() -> AsyncIterator[AsyncIterator[JobEvent]]` (async context manager), `listener_count -> int`
  - `event_stream(bus: EventBus, *, keepalive_seconds: float = KEEPALIVE_SECONDS) -> AsyncIterator[str]`
  - `KEEPALIVE_SECONDS: float = 15.0`
  - `router: APIRouter` with prefix `/events` exposing `GET /events/jobs`

- [ ] **Step 1: Write the failing test**

Create an empty `tests/web/__init__.py`, then `tests/web/test_sse.py`:

```python
import asyncio
import json

import pytest

from higgshole.jobs.events import JobEvent
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState
from higgshole.web import sse
from higgshole.web.sse import EventBus, event_stream


def _event(
    *,
    gen_id: str = "a3f21c9d4e07",
    state: GenerationState = GenerationState.RUNNING,
    error_reason: ErrorReason | None = None,
) -> JobEvent:
    return JobEvent(
        generation_id=gen_id,
        kind=GenerationKind.VIDEO,
        state=state,
        error_reason=error_reason,
        detail=None,
        at="2026-07-18T14:30:22.104883+00:00",
    )


def test_event_serialises_as_a_named_sse_frame():
    # The web layer re-exports the runners' event rather than redefining it,
    # so the two can never drift into merely duck-type compatibility.
    assert sse.JobEvent is JobEvent

    frame = _event().to_sse()

    assert frame.startswith("event: job\ndata: ")
    assert frame.endswith("\n\n")

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["generation_id"] == "a3f21c9d4e07"
    assert payload["state"] == "RUNNING"
    assert payload["kind"] == "video"


def test_the_frame_carries_the_machine_readable_failure_reason():
    frame = _event(
        state=GenerationState.FAILED, error_reason=ErrorReason.PROVIDER_EXPIRED
    ).to_sse()

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["error_reason"] == "provider_expired"


async def test_a_subscriber_receives_published_events():
    bus = EventBus()

    async with bus.subscribe() as events:
        bus.publish(_event(gen_id="000000000001"))
        received = await asyncio.wait_for(anext(events), timeout=1)

    assert received.generation_id == "000000000001"


async def test_every_subscriber_receives_the_same_event():
    bus = EventBus()

    async with bus.subscribe() as first, bus.subscribe() as second:
        assert bus.listener_count == 2
        bus.publish(_event())

        a = await asyncio.wait_for(anext(first), timeout=1)
        b = await asyncio.wait_for(anext(second), timeout=1)

    assert a == b


async def test_a_full_queue_drops_the_oldest_event():
    # A slow browser tab must never stall a job runner, so publish is
    # non-blocking and sheds the oldest event instead.
    bus = EventBus(max_queue=2)

    async with bus.subscribe() as events:
        bus.publish(_event(gen_id="000000000001"))
        bus.publish(_event(gen_id="000000000002"))
        bus.publish(_event(gen_id="000000000003"))

        first = await asyncio.wait_for(anext(events), timeout=1)
        second = await asyncio.wait_for(anext(events), timeout=1)

    assert [first.generation_id, second.generation_id] == [
        "000000000002",
        "000000000003",
    ]


async def test_leaving_the_subscription_removes_the_listener():
    bus = EventBus()

    async with bus.subscribe():
        assert bus.listener_count == 1

    assert bus.listener_count == 0
    bus.publish(_event())  # must not raise with no listeners


async def test_the_stream_emits_a_keepalive_comment_when_idle():
    # Proxies close idle connections; a comment line keeps the stream open
    # without inventing a job event.
    stream = event_stream(EventBus(), keepalive_seconds=0.01)

    try:
        assert await asyncio.wait_for(anext(stream), timeout=1) == ": keepalive\n\n"
    finally:
        await stream.aclose()


@pytest.mark.parametrize("state", [GenerationState.COMPLETE, GenerationState.FAILED])
async def test_the_stream_forwards_published_events(state):
    bus = EventBus()
    stream = event_stream(bus, keepalive_seconds=5)

    try:
        bus.publish(_event(state=state))
        # Give the generator a turn to register its subscription first.
        await asyncio.sleep(0)
        bus.publish(_event(state=state))
        frame = await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()

    assert f'"state": "{state.value}"' in frame
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_sse.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.web'`.

- [ ] **Step 3: Implement**

Extend `pyproject.toml` with the web dependencies (all of Plan 4 needs them, so add them once here).

**The dependency list is cumulative.** Plans 1–3 already added `httpx`, `pydantic`,
`pydantic-settings`, `pillow` and `anyio`; replacing the list with a shorter one
would make `uv sync` uninstall them — dropping `pillow` alone breaks
`store/metadata.py`'s `from PIL import Image`, every `tests/store/test_metadata_*`
test and every `probe_media` call. The block below is the complete expected result
after this edit:

```toml
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pillow>=10.3",
    "anyio>=4.4",
    "fastapi>=0.115",
    "starlette>=0.39",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "uvicorn>=0.30",
]
```

Create an empty `src/higgshole/web/__init__.py`.

Create `src/higgshole/web/sse.py`:

```python
"""Live job status over Server-Sent Events.

Fan-out is in-process because the deployment runs exactly one uvicorn worker
(spec section 9); a broker would buy nothing and add an operational
dependency to a single-user application.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from higgshole.jobs.events import JobEvent

#: How long the stream waits before emitting a comment line. Proxies commonly
#: close a connection idle for 30-60s, so this stays comfortably below that.
KEEPALIVE_SECONDS: float = 15.0

#: Re-exported, never redefined. `JobEvent` belongs to `jobs/` because the
#: dependency direction is web -> jobs (spec section 4.1), and the runners
#: construct that class. A second frozen dataclass with the same field names
#: would be a different type, so every `isinstance` and `Protocol` check
#: against `EventPublisher` would quietly mislead.
__all__ = [
    "KEEPALIVE_SECONDS",
    "EventBus",
    "JobEvent",
    "event_stream",
    "router",
]


class EventBus:
    """In-process fan-out from the job runners to every open browser tab."""

    def __init__(self, *, max_queue: int = 100) -> None:
        self._max_queue = max_queue
        self._listeners: set[asyncio.Queue[JobEvent]] = set()

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def publish(self, event: JobEvent) -> None:
        """Deliver to every listener without ever blocking.

        A listener whose queue is full loses its oldest event. Back-pressure
        onto a job runner would let a stalled tab delay a paid generation,
        which is a far worse failure than a missing status line.
        """
        for queue in list(self._listeners):
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    break
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[JobEvent]]:
        """Register a listener queue and remove it on exit."""
        queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=self._max_queue)
        self._listeners.add(queue)

        async def _iterate() -> AsyncIterator[JobEvent]:
            while True:
                yield await queue.get()

        iterator = _iterate()
        try:
            yield iterator
        finally:
            self._listeners.discard(queue)
            await iterator.aclose()


async def event_stream(
    bus: EventBus, *, keepalive_seconds: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    """Yield SSE text frames until the client disconnects.

    Kept separate from the route so the framing is testable without an
    application, a server, or a socket.
    """
    async with bus.subscribe() as events:
        iterator = events.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(
                    iterator.__anext__(), timeout=keepalive_seconds
                )
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:  # pragma: no cover - bus closed
                return
            yield event.to_sse()


router = APIRouter(prefix="/events", tags=["events"])


@router.get("/jobs")
async def stream_jobs(request: Request) -> StreamingResponse:
    """text/event-stream of every job state transition."""
    bus: EventBus = request.app.state.higgshole.events
    return StreamingResponse(
        event_stream(bus),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_sse.py -v`

Expected: PASS — `9 passed` (eight test functions, the last parametrized over
two states).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/higgshole/web/ tests/web/
git commit -m "feat: add in-process event bus and SSE job stream"
```

---

## Task 2: The mounted media application

**Files:**
- Create: `src/higgshole/web/media.py`
- Modify: `tests/conftest.py` (add shared `db` and `media_paths` fixtures)
- Test: `tests/web/test_media.py`

**Interfaces:**
- Consumes: `MediaPaths` (`resolve_within_root`, `videos_dir`, `thumbs_dir`, `ensure_project_tree`), `PathTraversalError`, `Database`, `store.metadata.mime_for`, `UnsupportedMediaError` (Plan 2).
- Produces:
  - `MEDIA_MOUNT_PATH: str = "/media"`, `THUMBS_MOUNT_PATH: str = "/thumbs"`
  - `create_media_app(paths: MediaPaths, db: Database) -> Starlette`
  - `async serve_media(request: Request) -> FileResponse`
  - `async serve_thumb(request: Request) -> FileResponse`
  - `media_url_for(relative_path: str) -> str`
  - `thumb_url_for(*, project_slug: str, gen_id: str) -> str`
  - `poster_url_for(*, project_slug: str, gen_id: str) -> str`

- [ ] **Step 1: Write the failing test**

Append to `tests/conftest.py` (do **not** create a second conftest — the network guard lives here and must not be shadowed):

```python
@pytest.fixture
def db():
    """A migrated in-memory database, closed after the test."""
    from higgshole.store.db import Database

    database = Database.in_memory()
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def media_paths(tmp_path):
    """A media root under tmp_path, with the default project tree created."""
    from higgshole.store.paths import MediaPaths

    paths = MediaPaths(tmp_path / "media")
    paths.ensure_project_tree("unsorted")
    return paths
```

Create `tests/web/test_media.py`:

```python
import pytest
from starlette.testclient import TestClient

from higgshole.web.media import (
    create_media_app,
    media_url_for,
    poster_url_for,
    thumb_url_for,
)

#: 2048 deterministic bytes, large enough for meaningful ranges.
PAYLOAD = bytes(range(256)) * 8


@pytest.fixture
def media_client(media_paths, db):
    video = media_paths.videos_dir("unsorted") / "clip.mp4"
    video.write_bytes(PAYLOAD)

    thumb = media_paths.thumbs_dir("unsorted") / "a3f21c9d4e07.webp"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"RIFF____WEBPVP8 ")

    with TestClient(create_media_app(media_paths, db)) as client:
        yield client


def test_a_full_request_returns_the_whole_file(media_client):
    response = media_client.get("/media/projects/unsorted/videos/clip.mp4")

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["accept-ranges"] == "bytes"


def test_a_range_request_returns_206_with_a_correct_content_range(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": "bytes=0-499"},
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-499/{len(PAYLOAD)}"
    assert response.headers["content-length"] == "500"
    assert response.content == PAYLOAD[:500]


def test_a_suffix_range_returns_the_final_bytes(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": "bytes=-500"},
    )

    assert response.status_code == 206
    assert response.content == PAYLOAD[-500:]
    start = len(PAYLOAD) - 500
    assert response.headers["content-range"] == (
        f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
    )


def test_an_unsatisfiable_range_returns_416(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": f"bytes={len(PAYLOAD) + 10}-{len(PAYLOAD) + 20}"},
    )

    assert response.status_code == 416


def test_a_missing_file_is_404(media_client):
    assert media_client.get("/media/projects/unsorted/videos/nope.mp4").status_code == 404


@pytest.mark.parametrize(
    "crafted",
    [
        "/media/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc/passwd",
        "/media/projects%2f..%2f..%2f..%2fetc/passwd",
    ],
)
def test_encoded_parent_traversal_is_rejected(media_client, crafted):
    response = media_client.get(crafted)

    # 404 rather than 403: a 403 would confirm that the target exists.
    assert response.status_code == 404
    assert b"root:" not in response.content


def test_an_absolute_path_cannot_escape_the_root(media_client):
    response = media_client.get("/media//etc/passwd")

    assert response.status_code == 404
    assert b"root:" not in response.content


def test_thumbnails_are_served_from_the_thumbs_tree(media_client):
    response = media_client.get("/thumbs/unsorted/a3f21c9d4e07.webp")

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")


def test_a_thumbnail_request_cannot_escape_its_project(media_client):
    response = media_client.get("/thumbs/unsorted/%2e%2e%2f%2e%2e%2fpasswd")

    assert response.status_code == 404


def test_url_helpers_are_the_single_place_urls_are_built():
    assert (
        media_url_for("projects/unsorted/images/20260718-143022_a3f21c9d4e07_x.png")
        == "/media/projects/unsorted/images/20260718-143022_a3f21c9d4e07_x.png"
    )
    assert (
        thumb_url_for(project_slug="unsorted", gen_id="a3f21c9d4e07")
        == "/thumbs/unsorted/a3f21c9d4e07.webp"
    )
    assert (
        poster_url_for(project_slug="unsorted", gen_id="a3f21c9d4e07")
        == "/thumbs/unsorted/a3f21c9d4e07_poster.webp"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_media.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.web.media'`.

- [ ] **Step 3: Implement**

Create `src/higgshole/web/media.py`:

```python
"""Media byte serving.

This is a standalone Starlette application rather than a set of routes on the
main app. `web/app.py` dispatches to it BEFORE the parent's middleware stack
runs, which is the structural guarantee behind spec section 6.3: no middleware
anyone adds later can compress or re-length a 206 response.

`FileResponse` implements HTTP Range natively from Starlette 0.39.0 — 206 with
Content-Range, suffix ranges and 416 all come for free, so there is no custom
byte-slicing code here to get wrong.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route

from higgshole.store.db import Database
from higgshole.store.metadata import UnsupportedMediaError, mime_for
from higgshole.store.paths import MediaPaths, PathTraversalError

MEDIA_MOUNT_PATH: str = "/media"
THUMBS_MOUNT_PATH: str = "/thumbs"


def media_url_for(relative_path: str) -> str:
    """Turn a media-root-relative path into its HTTP URL.

    The single place media URLs are built, so API responses and templates
    cannot drift apart.
    """
    return f"{MEDIA_MOUNT_PATH}/{str(relative_path).lstrip('/')}"


def thumb_url_for(*, project_slug: str, gen_id: str) -> str:
    return f"{THUMBS_MOUNT_PATH}/{project_slug}/{gen_id}.webp"


def poster_url_for(*, project_slug: str, gen_id: str) -> str:
    return f"{THUMBS_MOUNT_PATH}/{project_slug}/{gen_id}_poster.webp"


def _file_response(paths: MediaPaths, relative: str) -> FileResponse:
    """Resolve, contain, and serve one file.

    Containment failure is reported as 404, not 403: a 403 tells a caller that
    the crafted target exists, which is information the caller should not get.
    """
    try:
        target = paths.resolve_within_root(relative)
    except PathTraversalError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")

    try:
        media_type: str | None = mime_for(target)
    except UnsupportedMediaError:
        media_type = None

    return FileResponse(target, media_type=media_type)


async def serve_media(request: Request) -> FileResponse:
    """GET /media/{path:path} — any file beneath the media root."""
    paths: MediaPaths = request.app.state.paths
    return _file_response(paths, request.path_params["path"])


async def serve_thumb(request: Request) -> FileResponse:
    """GET /thumbs/{project_slug}/{filename}

    The project slug and filename are re-joined and then contained, so a
    crafted filename is caught by the same single guard as everything else.
    """
    paths: MediaPaths = request.app.state.paths
    slug = request.path_params["project_slug"]
    filename = request.path_params["filename"]
    relative = Path("thumbs") / slug / filename
    return _file_response(paths, relative)


def create_media_app(paths: MediaPaths, db: Database) -> Starlette:
    """Build the media sub-application.

    `db` is held for future needs (asset lookup by path) and to keep the
    signature stable; byte serving itself needs only the path allocator.
    """
    app = Starlette(
        routes=[
            Route(f"{MEDIA_MOUNT_PATH}/{{path:path}}", serve_media, methods=["GET", "HEAD"]),
            Route(
                f"{THUMBS_MOUNT_PATH}/{{project_slug}}/{{filename}}",
                serve_thumb,
                methods=["GET", "HEAD"],
            ),
        ]
    )
    app.state.paths = paths
    app.state.db = db
    return app
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_media.py -v`

Expected: PASS — `11 passed` (the traversal test is parametrized over two encodings).

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/media.py tests/conftest.py tests/web/test_media.py
git commit -m "feat: serve media by range from a standalone sub-application"
```

---

## Task 3: The application factory and lifespan

**Files:**
- Create: `src/higgshole/web/app.py`
- Create: `tests/web/fakes.py`
- Test: `tests/web/test_app.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings`; `Database`, `MediaPaths`; `CatalogCache`; `Ledger`, `BudgetGate`; `ImageJobRunner`, `VideoJobRunner`, `resume_pending_jobs`, `ResumeReport`; `EventBus` and the SSE `router` from Task 1; `create_media_app`, `MEDIA_MOUNT_PATH`, `THUMBS_MOUNT_PATH` from Task 2.
- Produces:
  - `AppState` — mutable dataclass holding `settings, db, paths, catalog, ledger, gate, image_runner, video_runner, events, resume_report, client_factory, key_status_cached`
  - `build_app_state(settings: Settings, db: Database | None = None) -> AppState`
  - `resolve_api_key(db: Database, settings: Settings, kind: MediaKind) -> str | None`
  - `resolve_daily_cap(db: Database, settings: Settings) -> Decimal | None`
  - `lifespan(app: FastAPI) -> AsyncIterator[None]`
  - `get_state(request: Request) -> AppState`
  - `create_app(*, settings: Settings | None = None, db: Database | None = None) -> HiggsHoleApp` — opens **exactly one** `Database` when none is injected, and registers the `HTTPException` handler that serialises structured errors at the top level
  - `HiggsHoleApp(FastAPI)` — dispatches media paths ahead of the middleware stack
  - `run() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/web/fakes.py` — the shared test doubles for every remaining task:

```python
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

from higgshole.budget.estimator import Estimate, EstimateUnavailable
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
        self._pricing = pricing if pricing is not None else [
            {"billable": "output_image", "unit": "image", "cost_usd": 0.04}
        ]
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
        self, reservation, *, actual_cost: Decimal | None = None, succeeded: bool = False
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

    def __init__(self, key_status: KeyStatus | None = None, error: Exception | None = None):
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
```

Create `tests/web/test_app.py`:

```python
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from higgshole.config import Settings
from higgshole.store.db import Database, GenerationKind, GenerationState
from higgshole.web.app import AppState, build_app_state, create_app, get_state, run
from tests.web.fakes import FakeCatalog, build_test_state


@pytest.fixture
def app_and_state(db, media_paths):
    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    return app, state


def test_the_factory_returns_a_fastapi_application(app_and_state):
    app, _ = app_and_state

    assert isinstance(app, FastAPI)


def test_the_state_dependency_returns_the_assembled_state(app_and_state):
    app, state = app_and_state

    with TestClient(app) as client:
        request_state = client.app.state.higgshole

    assert isinstance(request_state, AppState)
    assert request_state is state
    assert get_state.__name__ == "get_state"


def test_startup_migrates_and_creates_the_default_project(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        project = state.db.get_project_by_slug("unsorted")

    assert project is not None
    assert project.slug == "unsorted"


def test_startup_survives_a_failing_catalogue_refresh(db, media_paths):
    # Spec section 4.2: the application never blocks startup on catalogue
    # availability. A provider outage must not make the service unbootable.
    catalog = FakeCatalog(refresh_error=RuntimeError("provider down"))
    state = build_test_state(db=db, paths=media_paths, catalog=catalog)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app) as client:
        assert client.get("/events/jobs", headers={"Accept": "text/event-stream"}) is not None

    assert catalog.refresh_calls == 1


def test_startup_records_a_resume_report(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        report = state.resume_report

    assert report is not None
    assert report.reattached == ()


def test_startup_reattaches_a_video_row_left_mid_flight(db, media_paths):
    state = build_test_state(db=db, paths=media_paths)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.RUNNING,
    )
    db.set_provider_job_id(row.id, "job-abc")

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert row.id in (report.reattached + report.timed_out + report.orphaned)


def test_startup_releases_the_reservation_for_an_orphaned_video_row(db, media_paths):
    # A resumable row carrying no provider job ID is unrecoverable. Its
    # reservation must still be released, or the daily cap stays consumed by
    # a job that can never settle.
    state = build_test_state(db=db, paths=media_paths)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.SUBMITTED,
    )
    state.ledger.reserve(row.id, Decimal("0.50"))

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert report.orphaned == (row.id,)
    assert [succeeded for _, _, succeeded in state.video_runner.gate.released] == [False]


def test_startup_releases_the_reservation_for_a_timed_out_video_row(db, media_paths):
    # Past the wall-clock ceiling while the service was down: failed rather
    # than reattached, and the reservation reversed on the same branch.
    settings = Settings(
        media_root=media_paths.root,
        db_path=media_paths.root / "unused.db",
        daily_cap_usd=None,
        job_timeout_minutes=0,
    )
    state = build_test_state(db=db, paths=media_paths, settings=settings)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.RUNNING,
    )
    db.set_provider_job_id(row.id, "job-abc")
    state.ledger.reserve(row.id, Decimal("0.50"))

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert report.timed_out == (row.id,)
    assert report.reattached == ()
    assert [succeeded for _, _, succeeded in state.video_runner.gate.released] == [False]


def test_a_structured_error_is_serialised_at_the_top_level(app_and_state):
    # Both the browser and the MCP client read body["error"]; FastAPI's
    # default handler would nest the whole body under "detail" and break them.
    app, _ = app_and_state

    @app.get("/_test/structured")
    async def _structured() -> None:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "message": "bad", "issues": []},
        )

    with TestClient(app) as client:
        response = client.get("/_test/structured")

    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert "detail" not in response.json()


def test_a_plain_http_exception_still_returns_a_usable_body(app_and_state):
    # Starlette and FastAPI raise plain-string HTTPExceptions internally, so
    # the handler must give those the same two-field shape.
    app, _ = app_and_state

    @app.get("/_test/plain")
    async def _plain() -> None:
        raise HTTPException(status_code=404, detail="nothing here")

    with TestClient(app) as client:
        response = client.get("/_test/plain")

    assert response.status_code == 404
    assert response.json() == {"error": "http_error", "message": "nothing here"}


def test_only_one_database_is_opened_when_none_is_injected(monkeypatch, tmp_path):
    # Plan 2's Database wraps a single SQLite connection under a single
    # worker; a second one would be opened and never closed at shutdown.
    constructed: list[Database] = []
    original = Database.from_settings

    def counting(settings: Settings) -> Database:
        database = original(settings)
        constructed.append(database)
        return database

    monkeypatch.setattr(Database, "from_settings", counting)

    app = create_app(
        settings=Settings(
            media_root=tmp_path / "media",
            db_path=tmp_path / "higgshole.db",
            daily_cap_usd=None,
        )
    )

    with TestClient(app):
        pass

    assert len(constructed) == 1


def test_a_key_saved_through_the_ui_reaches_the_catalogue(monkeypatch, tmp_path):
    # The catalogue shares the resolving factory instead of building its own
    # from the environment; otherwise a refresh — and the lazy image-pricing
    # fetch behind it — would authenticate with a key the user never set and
    # fail with AuthError while generation worked fine.
    keys: list[str] = []

    def record(api_key: str) -> str:
        keys.append(api_key)
        return api_key

    monkeypatch.setattr("higgshole.web.app.OpenRouterClient", record)

    settings = Settings(
        media_root=tmp_path / "media",
        db_path=tmp_path / "higgshole.db",
        daily_cap_usd=None,
    )
    database = Database.from_settings(settings)
    database.migrate()
    database.set_setting("openrouter_api_key_video", "sk-or-v1-abcdef0123456789")

    state = build_app_state(settings, db=database)
    state.catalog.client_factory("video")

    assert keys == ["sk-or-v1-abcdef0123456789"]


def test_shutdown_cancels_every_poller(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        pass

    assert state.video_runner.shutdown_calls == 1


def test_the_entrypoint_runs_exactly_one_worker(monkeypatch):
    # Spec section 9: two workers would each reattach a poller to the same job
    # at boot, causing duplicate downloads and double-counted spend.
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))

    run()

    assert captured["workers"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_app.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.web.app'`.

- [ ] **Step 3: Implement**

Create `src/higgshole/web/app.py`:

```python
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
    """Assemble the real object graph. Tests substitute their own AppState."""
    database = db if db is not None else Database.from_settings(settings)
    paths = MediaPaths.from_settings(settings)
    events = EventBus()

    def client_factory(kind: MediaKind) -> OpenRouterClient:
        """A fresh client per call, with the key resolved at call time, so a
        key saved through the UI takes effect on the next request rather than
        at the next restart."""
        return OpenRouterClient(resolve_api_key(database, settings, kind) or "")

    # Not `CatalogCache.from_settings`: that builds its own environment-only
    # factory, so a key saved through the UI would never reach a catalogue
    # refresh or a lazy image-pricing fetch.
    catalog = CatalogCache(
        database, client_factory, ttl_hours=settings.catalog_ttl_hours
    )
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

    app.include_router(sse.router)
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_app.py -v`

Expected: PASS — `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/app.py tests/web/fakes.py tests/web/test_app.py
git commit -m "feat: add application factory, shared state and lifespan"
```

---

## Task 4: The 206 / Content-Encoding regression

**Files:**
- Test: `tests/web/test_media_middleware.py`

**Interfaces:**
- Consumes: `create_app` (Task 3), `MediaPaths` (Plan 2), `starlette.middleware.gzip.GZipMiddleware`.
- Produces: no new production symbols. This task proves the structural guarantee from spec §6.3 and §11 and adds no implementation of its own — if it fails, `HiggsHoleApp.__call__` is wrong.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_media_middleware.py`:

```python
"""The guarantee behind spec section 6.3.

GZipMiddleware compresses partial content while Content-Range continues to
describe the uncompressed entity, and it does not exempt video/mp4 — so a
browser seeking through a video gets corrupt bytes. HiggsHole never registers
it, but "we promise not to" is not a mechanism. These tests add the middleware
deliberately and prove media is still served untouched.
"""

import pytest
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tests.web.fakes import build_test_state

PAYLOAD = bytes(range(256)) * 40  # 10240 highly compressible bytes


@pytest.fixture
def gzipped_client(db, media_paths):
    from higgshole.web.app import create_app

    video = media_paths.videos_dir("unsorted") / "clip.mp4"
    video.write_bytes(PAYLOAD)

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    # Deliberately hostile: the exact middleware the spec forbids.
    app.add_middleware(GZipMiddleware, minimum_size=1)

    @app.get("/_bulk")
    async def _bulk() -> JSONResponse:
        return JSONResponse({"filler": "x" * 4096})

    with TestClient(app) as client:
        yield client


def test_the_middleware_really_is_active(gzipped_client):
    # Without this the other three assertions could pass vacuously.
    response = gzipped_client.get("/_bulk", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"


def test_a_full_media_response_is_never_compressed(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.content == PAYLOAD


def test_a_206_response_carries_no_content_encoding(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=100-599"},
    )

    assert response.status_code == 206
    assert "content-encoding" not in response.headers


def test_a_206_content_range_still_describes_the_bytes_returned(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=100-599"},
    )

    assert response.headers["content-range"] == f"bytes 100-599/{len(PAYLOAD)}"
    assert response.headers["content-length"] == "500"
    assert response.content == PAYLOAD[100:600]
```

- [ ] **Step 2: Run to verify it fails**

Temporarily comment out the media interception in `HiggsHoleApp.__call__` (the `if scope["type"] == "http"` block) and add `app.mount(MEDIA_MOUNT_PATH, ...)` in its place, then run:

Run: `uv run pytest tests/web/test_media_middleware.py -v`

Expected: FAIL — `KeyError: 'content-range'` and `assert 'content-encoding' not in ...` on the 206 tests, because mounting alone does not exempt a sub-application from parent middleware. Restore `HiggsHoleApp.__call__` before Step 3.

- [ ] **Step 3: Implement**

No new implementation is required — `HiggsHoleApp.__call__` from Task 3 already dispatches the media prefixes ahead of `Starlette.middleware_stack`. Add the guarantee to the module docstring of `src/higgshole/web/media.py` so the next reader knows which test defends it:

```python
"""Media byte serving.

This is a standalone Starlette application rather than a set of routes on the
main app. `web/app.py` dispatches to it BEFORE the parent's middleware stack
runs, which is the structural guarantee behind spec section 6.3: no middleware
anyone adds later can compress or re-length a 206 response. The regression
test in tests/web/test_media_middleware.py adds GZipMiddleware on purpose and
proves media responses are still uncompressed.

`FileResponse` implements HTTP Range natively from Starlette 0.39.0 — 206 with
Content-Range, suffix ranges and 416 all come for free, so there is no custom
byte-slicing code here to get wrong.
"""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_media_middleware.py -v`

Expected: PASS — `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/media.py tests/web/test_media_middleware.py
git commit -m "test: prove media responses bypass parent middleware"
```

---

## Task 5: REST models, masking, catalogue, projects and budget

**Files:**
- Create: `src/higgshole/web/api.py`
- Modify: `src/higgshole/web/app.py` (include the API router)
- Test: `tests/web/test_api_core.py`

**Interfaces:**
- Consumes: `AppState`, `get_state`; `CatalogCache.get_video_models/get_image_models`; `Database.list_projects/create_project/count_generations`; `BudgetGate.status(key_status)`; `KeyStatus`; `DuplicateSlugError`; the `OpenRouterError` hierarchy; `ValidationIssue`, `Severity`.
- Produces: `router: APIRouter` (prefix `/api`); the Pydantic models in the contract §10.3; `mask_key`, `error_response`, `map_openrouter_error`, `current_key_status`, `KEY_STATUS_TTL_SECONDS`; routes `GET /api/models`, `GET /api/projects`, `POST /api/projects`, `GET /api/budget`.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_api_core.py`:

```python
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from higgshole.orclient.errors import (
    InsufficientCreditsError,
    ModerationError,
    ProviderError,
)
from higgshole.orclient.types import KeyStatus
from higgshole.web.api import map_openrouter_error, mask_key
from tests.web.fakes import FakeClient, build_test_state


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


@pytest.mark.parametrize(
    "value",
    ["sk-or-v1-abcdef0123456789", "sk-or-v1-wxyz", None, ""],
)
def test_mask_key_never_reveals_more_than_four_characters(value):
    masked = mask_key(value)

    if not value:
        assert masked is None
        return

    assert masked is not None
    assert masked.startswith("...")
    assert len(masked.removeprefix("...")) <= 4
    assert masked.removeprefix("...") == value[-4:]
    assert value not in masked


def test_error_response_builds_a_uniform_body():
    from higgshole.catalog.validation import Severity, ValidationIssue
    from higgshole.web.api import error_response

    exc = error_response(
        422,
        "validation_failed",
        "bad request",
        issues=[
            ValidationIssue(
                parameter="duration",
                value="7",
                severity=Severity.HARD,
                message="unsupported",
            )
        ],
    )

    assert exc.status_code == 422
    assert exc.detail["error"] == "validation_failed"
    assert exc.detail["issues"][0]["parameter"] == "duration"


def test_a_provider_credit_limit_is_named_distinctly_from_the_local_cap():
    # Spec section 10: the operator must know which guard tripped.
    exc = map_openrouter_error(InsufficientCreditsError("out of credit", status_code=402))

    assert exc.status_code == 402
    assert exc.detail["error"] == "provider_credit_limit"


def test_a_moderation_refusal_has_its_own_code():
    exc = map_openrouter_error(ModerationError("content policy", status_code=400))

    assert exc.detail["error"] == "moderation_refused"


def test_an_upstream_failure_is_reported_as_provider_unavailable():
    exc = map_openrouter_error(ProviderError("upstream", status_code=502))

    assert exc.status_code == 502
    assert exc.detail["error"] == "provider_unavailable"


def test_models_are_returned_with_their_discovered_capabilities(api):
    client, _ = api

    payload = client.get("/api/models").json()
    by_id = {entry["id"]: entry for entry in payload}

    assert by_id["kwaivgi/kling-v3.0-pro"]["supported_durations"] == [5, 10]
    assert by_id["openai/sora-2-pro"]["supported_frame_images"] == []
    assert by_id["openai/gpt-image-2"]["max_input_references"] == 16


def test_models_can_be_filtered_by_kind(api):
    client, _ = api

    payload = client.get("/api/models", params={"kind": "video"}).json()

    assert {entry["kind"] for entry in payload} == {"video"}


def test_favourite_models_are_flagged(api):
    client, state = api
    state.db.set_setting("favourite_models", '["openai/sora-2-pro"]')

    payload = client.get("/api/models").json()
    favourites = {entry["id"] for entry in payload if entry["is_favourite"]}

    assert favourites == {"openai/sora-2-pro"}


def test_projects_can_be_listed_and_created(api):
    client, _ = api

    created = client.post("/api/projects", json={"name": "Coast Shoot"})
    assert created.status_code == 201
    assert created.json()["slug"] == "coast-shoot"

    slugs = {entry["slug"] for entry in client.get("/api/projects").json()}
    assert {"unsorted", "coast-shoot"} <= slugs


def test_a_duplicate_project_is_rejected_with_409(api):
    client, _ = api
    client.post("/api/projects", json={"name": "Coast Shoot"})

    conflict = client.post("/api/projects", json={"name": "Coast Shoot"})

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "validation_failed"


def test_budget_reports_the_provider_figures_as_strings(api):
    # Spec section 3.2: provider figures are authoritative, and money crosses
    # the boundary as a string so no float rounding can occur.
    client, _ = api

    payload = client.get("/api/budget").json()

    assert payload["provider_available"] is True
    assert payload["provider_remaining_usd"] == "74.5"
    assert payload["cap_usd"] is None
    assert payload["spent_today_usd"] == "0"
    assert payload["max_in_flight"] == 3


def test_budget_marks_the_figures_local_only_when_the_key_call_fails(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(
        db=db,
        paths=media_paths,
        client=FakeClient(error=ProviderError("down", status_code=503)),
    )
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app) as client:
        payload = client.get("/api/budget").json()

    assert payload["provider_available"] is False
    assert payload["provider_remaining_usd"] is None


def test_the_key_status_is_cached_between_requests(api, monkeypatch):
    client, state = api
    calls = {"n": 0}
    original = state.client_factory("image").get_key_status

    async def counting() -> KeyStatus:
        calls["n"] += 1
        return await original()

    monkeypatch.setattr(state.client_factory("image"), "get_key_status", counting)

    client.get("/api/budget")
    client.get("/api/budget")

    assert calls["n"] == 1
    assert Decimal(client.get("/api/budget").json()["provider_limit_usd"]) == Decimal("100")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_api_core.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.web.api'`.

- [ ] **Step 3: Implement**

Create `src/higgshole/web/api.py`:

```python
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
            _video_capability(m, favourites) for m in await state.catalog.get_video_models()
        ]
    if kind in (None, GenerationKind.IMAGE):
        entries += [
            _image_capability(m, favourites) for m in await state.catalog.get_image_models()
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
```

In `src/higgshole/web/app.py`, import the API router and include it. Extend the
existing `from higgshole.web import sse` line to:

```python
from higgshole.web import api, sse
```

and, at the bottom of `create_app`, replace `app.include_router(sse.router)` with:

```python
    from higgshole.web import api  # imported here to avoid a circular import

    app.include_router(api.router)
    app.include_router(sse.router)
    return app
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_api_core.py -v`

Expected: PASS — `16 passed` (the masking test is parametrized over four values).

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/api.py src/higgshole/web/app.py tests/web/test_api_core.py
git commit -m "feat: add REST models, key masking, catalogue, projects and budget"
```

---

## Task 6: Estimate, generation and job endpoints

**Files:**
- Modify: `src/higgshole/web/api.py`
- Test: `tests/web/test_api_generate.py`

**Interfaces:**
- Consumes: `estimate_image_cost`, `estimate_video_cost`, `Estimate`; `GenerationRequest`, `GenerationOutcome`; `ImageJobRunner.validate/run`; `VideoJobRunner.validate/submit`; `has_hard_failure`; `MediaFilter`, `TERMINAL_STATES`.
- Produces: `GenerateImageIn`, `GenerateVideoIn`; routes `POST /api/estimate`, `POST /api/generate/image`, `POST /api/generate/video`, `GET /api/jobs/{gen_id}`, `GET /api/jobs`; `JOB_POLL_INTERVAL_S`.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_api_generate.py`:

```python
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from higgshole.catalog.validation import Severity, ValidationIssue
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState
from tests.web.fakes import build_test_state, failed_outcome


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_an_exact_estimate_is_returned_as_a_string(api):
    client, _ = api

    payload = client.post(
        "/api/estimate",
        params={"kind": "image"},
        json={"model": "openai/gpt-image-2", "prompt": "a cat"},
    ).json()

    assert payload["estimate_unavailable"] is None
    assert isinstance(payload["amount_usd"], str)


def test_an_unavailable_estimate_returns_null_and_a_reason(api, monkeypatch):
    # Spec section 3.2 Layer 3: never a fabricated number.
    from higgshole.budget.estimator import Estimate, EstimateUnavailable
    from higgshole.web import api as api_module

    monkeypatch.setattr(
        api_module,
        "estimate_image_cost",
        lambda *a, **k: Estimate(
            amount=None,
            reason=EstimateUnavailable.TOKEN_PRICED,
            detail="priced per token with no published conversion",
        ),
    )
    client, _ = api

    payload = client.post(
        "/api/estimate",
        params={"kind": "image"},
        json={"model": "openai/gpt-image-2", "prompt": "a cat"},
    ).json()

    assert payload["amount_usd"] is None
    assert payload["estimate_unavailable"] == "token_priced"


def test_image_generation_returns_the_finished_generation(api):
    client, state = api

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "neon city", "quality": "high"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "COMPLETE"
    assert body["project_slug"] == "unsorted"
    assert state.image_runner.requests[0].params["quality"] == "high"


def test_a_hard_validation_failure_is_422_with_the_issues(api):
    client, state = api
    state.image_runner.issues = [
        ValidationIssue(
            parameter="quality",
            value="ultra",
            severity=Severity.HARD,
            message="openai/gpt-image-2 does not support quality=ultra.",
        )
    ]

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "x", "quality": "ultra"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_failed"
    assert body["issues"][0]["parameter"] == "quality"
    assert state.image_runner.requests == []


def test_an_advisory_issue_does_not_block_dispatch(api):
    # Spec section 2.7: a value the catalogue omits but pricing covers is
    # warned about and sent.
    client, state = api
    state.image_runner.issues = [
        ValidationIssue(
            parameter="resolution",
            value="1080p",
            severity=Severity.ADVISORY,
            message="not declared but priced",
        )
    ]

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 200
    assert len(state.image_runner.requests) == 1


def test_a_cap_rejection_is_402_local_daily_cap(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.image_runner.outcome = failed_outcome(
        state.db,
        project.id,
        reason=ErrorReason.CAP_EXCEEDED,
        state=GenerationState.REJECTED,
    )

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 402
    assert response.json()["error"] == "local_daily_cap"


def test_an_in_flight_rejection_is_429(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.image_runner.outcome = failed_outcome(
        state.db,
        project.id,
        reason=ErrorReason.IN_FLIGHT_LIMIT,
        state=GenerationState.REJECTED,
    )

    response = client.post(
        "/api/generate/image", json={"model": "openai/gpt-image-2", "prompt": "x"}
    )

    assert response.status_code == 429
    assert response.json()["error"] == "in_flight_limit"


def test_video_submission_returns_202_without_blocking(api):
    # Spec section 6.2: a multi-minute render inside one call invites timeouts.
    client, state = api

    response = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach", "duration": 5},
    )

    assert response.status_code == 202
    assert response.json()["state"] == "SUBMITTED"


def test_an_unknown_model_is_404_model_not_found(api):
    client, _ = api

    response = client.post(
        "/api/generate/image", json={"model": "nope/nothing", "prompt": "x"}
    )

    assert response.status_code == 404
    assert response.json()["error"] == "model_not_found"


def test_an_unknown_project_is_404_project_not_found(api):
    client, _ = api

    response = client.post(
        "/api/generate/image",
        json={"model": "openai/gpt-image-2", "prompt": "x", "project": "ghost"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "project_not_found"


def test_get_job_returns_the_current_state(api):
    client, state = api
    created = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach"},
    ).json()

    fetched = client.get(f"/api/jobs/{created['id']}").json()

    assert fetched["id"] == created["id"]
    assert fetched["state"] == "SUBMITTED"


def test_get_job_long_polls_until_the_state_is_terminal(api, monkeypatch):
    client, state = api
    created = client.post(
        "/api/generate/video",
        json={"model": "kwaivgi/kling-v3.0-pro", "prompt": "a beach"},
    ).json()

    from higgshole.web import api as api_module

    monkeypatch.setattr(api_module, "JOB_POLL_INTERVAL_S", 0.01)

    calls = {"n": 0}
    real = state.db.get_generation

    def eventually_complete(gen_id: str):
        calls["n"] += 1
        row = real(gen_id)
        if row is not None and calls["n"] >= 3:
            return replace(row, state=GenerationState.COMPLETE)
        return row

    monkeypatch.setattr(state.db, "get_generation", eventually_complete)

    fetched = client.get(
        f"/api/jobs/{created['id']}", params={"wait_seconds": 5}
    ).json()

    assert fetched["state"] == "COMPLETE"
    assert calls["n"] >= 3


def test_an_unknown_job_is_404_generation_not_found(api):
    client, _ = api

    response = client.get("/api/jobs/000000000000")

    assert response.status_code == 404
    assert response.json()["error"] == "generation_not_found"


def test_listing_jobs_returns_only_in_flight_generations(api):
    client, state = api
    project = state.db.get_project_by_slug("unsorted")
    state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="finished",
        params={},
        state=GenerationState.COMPLETE,
    )
    running = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="in flight",
        params={},
        state=GenerationState.RUNNING,
    )

    ids = {entry["id"] for entry in client.get("/api/jobs").json()}

    assert running.id in ids
    assert all(entry["state"] != "COMPLETE" for entry in client.get("/api/jobs").json())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_api_generate.py -v`

Expected: FAIL — `assert 404 == 200` / `Not Found` for every route, because `/api/estimate`, `/api/generate/image`, `/api/generate/video` and `/api/jobs` do not exist yet.

- [ ] **Step 3: Implement**

Add to the imports at the top of `src/higgshole/web/api.py`:

```python
import asyncio

from fastapi import Body, Response

from higgshole.budget.estimator import Estimate, estimate_image_cost, estimate_video_cost
from higgshole.catalog.validation import has_hard_failure
from higgshole.jobs.runner import GenerationOutcome, GenerationRequest
from higgshole.store.db import TERMINAL_STATES, MediaFilter
```

Append to `src/higgshole/web/api.py`:

```python
#: How often a long-poll re-reads the row. Short enough to feel immediate,
#: long enough that a five-minute wait is 600 cheap reads, not 300,000.
JOB_POLL_INTERVAL_S: float = 0.5

#: Non-terminal states, derived so a new state cannot be forgotten here.
_IN_FLIGHT_STATES = tuple(
    s for s in GenerationState if s not in TERMINAL_STATES
)


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
    fields = ("duration", "resolution", "aspect_ratio", "size", "generate_audio", "seed")
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
        mapped = _REASON_STATUS.get(outcome.error_reason) if outcome.error_reason else None
        if mapped is not None:
            status, code = mapped
            raise error_response(
                status, code, outcome.error_detail or outcome.state.value
            )

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
            422, "validation_failed", "The request violates the model's constraints.",
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
            422, "validation_failed", "The request violates the model's constraints.",
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_api_generate.py -v`

Expected: PASS — `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/api.py tests/web/test_api_generate.py
git commit -m "feat: add estimate, generation and job endpoints"
```

---

## Task 7: Uploads, library browse, settings and rescan

**Files:**
- Modify: `src/higgshole/web/api.py`
- Modify: `src/higgshole/budget/gate.py` (runtime cap setter)
- Test: `tests/web/test_api_library.py`

**Interfaces:**
- Consumes: `MediaPaths.allocate_upload`; `atomic_write_bytes`, `delete_quietly`, `iter_sidecars`, `read_sidecar`, `SidecarError`; `probe_media`, `mime_for`, `extension_for`, `UnsupportedMediaError`, `ffmpeg_available`; `Database` asset/generation/settings methods; `looks_like_openrouter_key`; `video_references_supported`, `ReferenceTransport`; `resolve_daily_cap` (Task 3).
- Produces: `BudgetGate.set_daily_cap(cap: Decimal | None) -> None`; `SettingsIn`, `SettingsOut`, `RescanOut`, `MAX_UPLOAD_BYTES`, `rescan_library`; routes `POST /api/uploads`, `GET /api/media`, `GET /api/media/{gen_id}`, `DELETE /api/media/{gen_id}`, `GET /api/settings`, `PUT /api/settings`, `POST /api/settings/catalog/refresh`, `POST /api/settings/rescan`.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_api_library.py`:

```python
import json
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def api(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def _make_generation(state, *, kind=GenerationKind.IMAGE, prompt="neon city"):
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=kind,
        model="openai/gpt-image-2",
        prompt=prompt,
        params={"quality": "high"},
        state=GenerationState.COMPLETE,
    )
    relative = f"projects/unsorted/images/{row.id}.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    state.db.set_generation_file(row.id, relative)
    asset = state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )
    return row, asset


def test_uploading_a_file_creates_an_upload_asset(api):
    client, state = api

    response = client.post(
        "/api/uploads",
        files={"file": ("reference.png", PNG_1X1, "image/png")},
        data={"project": "unsorted"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "upload"
    assert body["mime_type"] == "image/png"
    assert body["url"].startswith("/media/projects/unsorted/uploads/")
    assert (state.paths.root / body["url"].removeprefix("/media/")).is_file()


def test_an_unsupported_upload_type_is_415(api):
    client, _ = api

    response = client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


def test_an_oversized_upload_is_413(api, monkeypatch):
    from higgshole.web import api as api_module

    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 8)
    client, _ = api

    response = client.post(
        "/api/uploads", files={"file": ("reference.png", PNG_1X1, "image/png")}
    )

    assert response.status_code == 413
    assert response.json()["error"] == "upload_too_large"


def test_media_listing_paginates_and_reports_the_total(api):
    client, state = api
    for index in range(3):
        _make_generation(state, prompt=f"item {index}")

    payload = client.get("/api/media", params={"limit": 2, "offset": 0}).json()

    assert payload["total"] == 3
    assert len(payload["items"]) == 2
    assert payload["limit"] == 2


def test_media_listing_filters_by_kind(api):
    client, state = api
    _make_generation(state, kind=GenerationKind.IMAGE)
    _make_generation(state, kind=GenerationKind.VIDEO)

    payload = client.get("/api/media", params={"kind": "video"}).json()

    assert {item["kind"] for item in payload["items"]} == {"video"}


def test_media_detail_includes_the_asset_and_its_urls(api):
    client, state = api
    row, _ = _make_generation(state)

    body = client.get(f"/api/media/{row.id}").json()

    assert body["asset"]["url"].startswith("/media/")
    assert body["asset"]["local_path"].endswith(f"{row.id}.png")
    assert body["thumb_url"] == f"/thumbs/unsorted/{row.id}.webp"


def test_media_detail_includes_lineage(api):
    client, state = api
    parent, parent_asset = _make_generation(state, prompt="parent")
    child, _ = _make_generation(state, prompt="child")
    state.db.add_generation_input(
        generation_id=child.id,
        asset_id=parent_asset.id,
        role="input_reference",
        position=0,
    )

    body = client.get(f"/api/media/{child.id}").json()

    assert body["inputs"][0]["asset_id"] == parent_asset.id
    assert body["inputs"][0]["generation_id"] == parent.id


def test_deleting_media_removes_the_row_and_the_file(api):
    client, state = api
    row, _ = _make_generation(state)
    on_disk = state.paths.root / f"projects/unsorted/images/{row.id}.png"

    response = client.delete(f"/api/media/{row.id}")

    assert response.status_code == 204
    assert state.db.get_generation(row.id) is None
    assert not on_disk.exists()


def test_deleting_unknown_media_is_404(api):
    client, _ = api

    response = client.delete("/api/media/000000000000")

    assert response.status_code == 404
    assert response.json()["error"] == "generation_not_found"


def test_settings_never_return_a_full_api_key(api):
    # Spec section 7: keys are write-only through the UI.
    client, state = api
    secret = "sk-or-v1-abcdef0123456789"
    state.db.set_setting("openrouter_api_key", secret)

    body = client.get("/api/settings").json()

    assert body["openrouter_api_key_masked"] == "...6789"
    assert secret not in json.dumps(body)


def test_saving_a_key_stores_it_and_returns_only_a_mask(api):
    client, state = api
    secret = "sk-or-v1-zyxwvu9876543210"

    body = client.put("/api/settings", json={"openrouter_api_key": secret}).json()

    assert body["openrouter_api_key_masked"] == "...3210"
    assert secret not in json.dumps(body)
    assert state.db.get_setting("openrouter_api_key") == secret


def test_a_malformed_key_is_rejected_before_it_is_stored(api):
    # A key from another provider otherwise yields the provider's misleading
    # "Missing Authentication header" (spec section 7).
    client, state = api

    response = client.put(
        "/api/settings", json={"openrouter_api_key": "sk-proj-abcdef0123456789"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "validation_failed"
    assert state.db.get_setting("openrouter_api_key") is None


def test_a_key_saved_through_the_api_is_used_by_the_next_generation(api):
    # Spec section 8: keys may alternatively be set through the UI, so the
    # client factory resolves through the database on every call instead of
    # reading the environment once at startup.
    from higgshole.web.app import resolve_api_key

    client, state = api
    secret = "sk-or-v1-abcdef0123456789"

    client.put("/api/settings", json={"openrouter_api_key_image": secret})

    assert resolve_api_key(state.db, state.settings, "image") == secret


def test_a_cap_saved_through_the_api_is_enforced(api):
    # The runners hold the gate, so a saved cap has to reach that live
    # instance; otherwise the UI accepts a cap that guards nothing.
    client, state = api

    client.put("/api/settings", json={"daily_cap_usd": "1.50"})

    assert state.gate.cap == Decimal("1.50")
    assert state.gate.cap_is_set is True

    client.put("/api/settings", json={"daily_cap_usd": ""})

    assert state.gate.cap is None


def test_settings_report_catalogue_freshness_and_transport(api):
    client, _ = api

    body = client.get("/api/settings").json()

    assert body["catalog"]["is_stale"] is False
    assert body["reference_transport"] == "data_uri"
    assert isinstance(body["video_references_supported"], bool)


def test_a_manual_catalogue_refresh_reports_its_status(api):
    client, state = api

    body = client.post("/api/settings/catalog/refresh").json()

    assert body["is_stale"] is False
    assert state.catalog.refresh_calls >= 1


def test_rescan_rebuilds_rows_from_sidecars(api):
    client, state = api
    relative = "projects/unsorted/images/20260718-143022_a3f21c9d4e07_neon.png"
    media = state.paths.root / relative
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(PNG_1X1)
    sidecar = media.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "sidecar_version": 1,
                "id": "a3f21c9d4e07",
                "kind": "image",
                "project_slug": "unsorted",
                "model": "openai/gpt-image-2",
                "prompt": "neon city street at night",
                "params": {"quality": "high"},
                "inputs": [],
                "provider": {"job_id": None, "generation_id": "gen-1"},
                "media": {
                    "relative_path": relative,
                    "mime_type": "image/png",
                    "bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "duration_s": None,
                },
                "cost": {"amount_usd": "0.04", "known": True},
                "created_at": "2026-07-18T14:30:22.104883+00:00",
                "completed_at": "2026-07-18T14:30:29.551204+00:00",
            }
        ),
        encoding="utf-8",
    )

    body = client.post("/api/settings/rescan").json()

    assert body["sidecars_read"] == 1
    assert body["generations_created"] == 1
    assert body["errors"] == []
    assert state.db.get_generation("a3f21c9d4e07") is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_api_library.py -v`

Expected: FAIL — `assert 404 == 201` on the upload test and `Not Found` for `/api/media`, `/api/settings` and `/api/settings/rescan`.

- [ ] **Step 3: Implement**

Add to the imports at the top of `src/higgshole/web/api.py`:

```python
from fastapi import File, Form, UploadFile

from higgshole.jobs.references import ReferenceTransport, video_references_supported
from higgshole.orclient.client import looks_like_openrouter_key
from higgshole.store.files import (
    SidecarError,
    atomic_write_bytes,
    delete_quietly,
    iter_sidecars,
    read_sidecar,
)
from higgshole.store.metadata import (
    UnsupportedMediaError,
    extension_for,
    ffmpeg_available,
    mime_for,
    probe_media,
)
from higgshole.store.paths import new_id
from higgshole.web.app import resolve_daily_cap
```

Add the runtime cap setter to `BudgetGate` in `src/higgshole/budget/gate.py`, next
to the `cap` property:

```python
    def set_daily_cap(self, cap: Decimal | None) -> None:
        """Replace the cap without rebuilding the gate.

        The cap may be saved through the UI (spec section 8) while runners
        already hold this instance, so it has to be mutable in place. The
        assignment is atomic and `acquire` reads it under the lock, so a
        concurrent reservation sees either the old cap or the new one.
        """
        self._cap = cap
```

Append to `src/higgshole/web/api.py`:

```python
#: 256 MiB. Large enough for any reference image or short clip an operator
#: would ingest, small enough that a mistake cannot fill the media root.
MAX_UPLOAD_BYTES: int = 256 * 1024 * 1024

_KEY_SETTINGS = (
    "openrouter_api_key",
    "openrouter_api_key_image",
    "openrouter_api_key_video",
)


class SettingsIn(BaseModel):
    openrouter_api_key: str | None = None
    openrouter_api_key_image: str | None = None
    openrouter_api_key_video: str | None = None
    daily_cap_usd: str | None = None
    favourite_models: list[str] | None = None


class SettingsOut(BaseModel):
    """Keys are write-only: only a masked suffix is ever returned."""

    openrouter_api_key_masked: str | None
    openrouter_api_key_image_masked: str | None
    openrouter_api_key_video_masked: str | None
    daily_cap_usd: str | None
    favourite_models: list[str]
    catalog: CatalogStatusOut
    ffmpeg_available: bool
    reference_transport: str
    video_references_supported: bool


class RescanOut(BaseModel):
    projects_created: int
    generations_created: int
    assets_created: int
    sidecars_read: int
    errors: list[str]


@router.post("/uploads", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    project: str = Form(default="unsorted"),
    state: AppState = Depends(get_state),
) -> AssetOut:
    """Ingest a local file so it can be used as a generation reference.

    Without this an agent holding an image on disk has no way to reference it
    (spec section 6.2).
    """
    target_project = _require_project(state, project)
    original_name = file.filename or "upload"

    try:
        mime_type = mime_for(original_name)
        extension = extension_for(mime_type)
    except UnsupportedMediaError as exc:
        raise error_response(
            415, "unsupported_media_type", f"{original_name!r} is not a supported type."
        ) from exc

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise error_response(
            413,
            "upload_too_large",
            f"{len(data)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte limit.",
        )

    asset_id = new_id()
    allocated = state.paths.allocate_upload(
        project_slug=target_project.slug,
        asset_id=asset_id,
        original_name=original_name,
        ext=extension,
    )
    atomic_write_bytes(allocated.media_path, data)

    metadata = probe_media(allocated.media_path)
    asset = state.db.create_asset(
        kind=AssetKind.UPLOAD,
        file_path=allocated.relative_media_path.as_posix(),
        mime_type=metadata.mime_type,
        bytes_=metadata.bytes,
        width=metadata.width,
        height=metadata.height,
        duration_s=metadata.duration_s,
        asset_id=asset_id,
    )
    return _asset_out(state, asset)


@router.get("/media", response_model=MediaListOut)
async def list_media(
    project: str | None = Query(default=None),
    kind: GenerationKind | None = Query(default=None),
    model: str | None = Query(default=None),
    created_after: str | None = Query(default=None),
    created_before: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    state: AppState = Depends(get_state),
) -> MediaListOut:
    filters = MediaFilter(
        project_slug=project,
        kind=kind,
        model=model,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    rows = state.db.list_generations(filters)
    return MediaListOut(
        items=[generation_out(state, row) for row in rows],
        total=state.db.count_generations(filters),
        limit=limit,
        offset=offset,
    )


@router.get("/media/{gen_id}", response_model=GenerationOut)
async def get_media(gen_id: str, state: AppState = Depends(get_state)) -> GenerationOut:
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")
    return generation_out(state, row)


@router.delete("/media/{gen_id}", status_code=204)
async def delete_media(gen_id: str, state: AppState = Depends(get_state)) -> Response:
    """Remove a generation, its files and its thumbnails.

    The database returns the paths and this handler unlinks them: `store/`
    never touches the disk on behalf of a request.
    """
    if state.db.get_generation(gen_id) is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")

    for relative in state.db.delete_generation(gen_id):
        absolute = state.paths.root / relative
        delete_quietly(absolute)
        delete_quietly(state.paths.sidecar_path(absolute))

    return Response(status_code=204)


def _settings_out(state: AppState) -> SettingsOut:
    stored = state.db.all_settings()
    favourites_raw = stored.get(FAVOURITES_SETTING)
    try:
        favourites = list(json.loads(favourites_raw)) if favourites_raw else []
    except (ValueError, TypeError):
        favourites = []

    status = state.catalog.status()
    return SettingsOut(
        openrouter_api_key_masked=mask_key(stored.get("openrouter_api_key")),
        openrouter_api_key_image_masked=mask_key(stored.get("openrouter_api_key_image")),
        openrouter_api_key_video_masked=mask_key(stored.get("openrouter_api_key_video")),
        daily_cap_usd=stored.get("daily_cap_usd"),
        favourite_models=favourites,
        catalog=CatalogStatusOut(
            image_fetched_at=status.image_fetched_at,
            video_fetched_at=status.video_fetched_at,
            is_stale=status.is_stale,
            last_error=status.last_error,
        ),
        ffmpeg_available=ffmpeg_available(),
        reference_transport=state.settings.reference_transport,
        video_references_supported=video_references_supported(
            ReferenceTransport(state.settings.reference_transport)
        ),
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings_view(state: AppState = Depends(get_state)) -> SettingsOut:
    return _settings_out(state)


@router.put("/settings", response_model=SettingsOut)
async def update_settings(
    body: SettingsIn, state: AppState = Depends(get_state)
) -> SettingsOut:
    """Persist settings. Keys are validated for shape before being stored."""
    for field in _KEY_SETTINGS:
        value = getattr(body, field)
        if value is None:
            continue
        if value == "":
            state.db.delete_setting(field)
            continue
        if not looks_like_openrouter_key(value):
            raise error_response(
                400,
                "validation_failed",
                "That does not look like an OpenRouter key; it must begin "
                "'sk-or-v1-'.",
            )
        state.db.set_setting(field, value.strip())

    if body.daily_cap_usd is not None:
        if body.daily_cap_usd == "":
            state.db.delete_setting("daily_cap_usd")
        else:
            try:
                Decimal(body.daily_cap_usd)
            except ArithmeticError as exc:
                raise error_response(
                    400, "validation_failed", "The daily cap must be a decimal amount."
                ) from exc
            state.db.set_setting("daily_cap_usd", body.daily_cap_usd)

        # The runners hold this gate instance, so the cap is applied to the
        # live object. Rebinding state.gate would leave them on the old one,
        # and waiting for a restart would leave the saved cap unenforced.
        state.gate.set_daily_cap(resolve_daily_cap(state.db, state.settings))

    if body.favourite_models is not None:
        state.db.set_setting(FAVOURITES_SETTING, json.dumps(body.favourite_models))

    # A rotated key must not be masked by a cached provider figure.
    state.key_status_cached = None
    return _settings_out(state)


@router.post("/settings/catalog/refresh", response_model=CatalogStatusOut)
async def refresh_catalog(state: AppState = Depends(get_state)) -> CatalogStatusOut:
    status = await state.catalog.refresh(force=True)
    return CatalogStatusOut(
        image_fetched_at=status.image_fetched_at,
        video_fetched_at=status.video_fetched_at,
        is_stale=status.is_stale,
        last_error=status.last_error,
    )


def rescan_library(state: AppState) -> RescanOut:
    """Rebuild index rows from the sidecars on disk (spec section 5.3).

    Only `projects`, `generations`, `assets` and `generation_inputs` are
    recoverable. `spend_ledger` and `settings` exist nowhere on disk, so a
    rescan is not a substitute for backing up the state directory.
    """
    projects_created = generations_created = assets_created = sidecars_read = 0
    errors: list[str] = []

    for sidecar_path in iter_sidecars(state.paths.root):
        try:
            payload = read_sidecar(sidecar_path)
        except SidecarError as exc:
            errors.append(f"{sidecar_path.name}: {exc}")
            continue

        sidecars_read += 1
        try:
            slug = payload["project_slug"]
            project = state.db.get_project_by_slug(slug)
            if project is None:
                project = state.db.create_project(name=slug, slug=slug)
                projects_created += 1

            gen_id = payload["id"]
            if state.db.get_generation(gen_id) is None:
                state.db.create_generation(
                    project_id=project.id,
                    kind=GenerationKind(payload["kind"]),
                    model=payload["model"],
                    prompt=payload["prompt"],
                    params=payload["params"],
                    state=GenerationState.COMPLETE,
                    gen_id=gen_id,
                )
                generations_created += 1

            media = payload["media"]
            relative = media["relative_path"]
            state.db.set_generation_file(gen_id, relative)
            if state.db.get_asset_by_path(relative) is None:
                state.db.create_asset(
                    kind=AssetKind.OUTPUT,
                    file_path=relative,
                    mime_type=media["mime_type"],
                    bytes_=media["bytes"],
                    generation_id=gen_id,
                    width=media["width"],
                    height=media["height"],
                    duration_s=media["duration_s"],
                )
                assets_created += 1

            for entry in payload["inputs"]:
                asset = state.db.get_asset_by_path(entry["relative_path"])
                if asset is not None:
                    state.db.add_generation_input(
                        generation_id=gen_id,
                        asset_id=asset.id,
                        role=InputRole(entry["role"]),
                        position=entry["position"],
                    )
        except (KeyError, ValueError) as exc:
            errors.append(f"{sidecar_path.name}: {exc}")

    return RescanOut(
        projects_created=projects_created,
        generations_created=generations_created,
        assets_created=assets_created,
        sidecars_read=sidecars_read,
        errors=errors,
    )


@router.post("/settings/rescan", response_model=RescanOut)
async def rescan(state: AppState = Depends(get_state)) -> RescanOut:
    return rescan_library(state)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_api_library.py -v`

Expected: PASS — `17 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/budget/gate.py src/higgshole/web/api.py tests/web/test_api_library.py
git commit -m "feat: add uploads, library browse, settings and rescan"
```

---

## Task 8: The five screens

**Files:**
- Create: `src/higgshole/web/pages.py`
- Create: `src/higgshole/web/templates/base.html`, `create.html`, `library.html`, `detail.html`, `jobs.html`, `settings.html`
- Create: `src/higgshole/web/static/app.css`
- Create: `src/higgshole/web/static/vendor/htmx.min.js`
- Modify: `src/higgshole/web/app.py` (include the pages router, mount static files)
- Test: `tests/web/test_pages.py`

**Interfaces:**
- Consumes: `AppState`, `get_state`; the API helpers `generation_out`, `_settings_out`, `_budget_out`, `current_key_status`; `media_url_for`, `thumb_url_for`, `poster_url_for`.
- Produces: `router: APIRouter` (no prefix), `templates: Jinja2Templates`, `TEMPLATES_DIR`, `STATIC_DIR`; routes `GET /`, `GET /library`, `GET /library/{gen_id}`, `GET /jobs`, `GET /settings`.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_pages.py`:

```python
import re

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from higgshole.web.pages import TEMPLATES_DIR
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def pages(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def _completed_generation(state, prompt="neon city street"):
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt=prompt,
        params={"quality": "high"},
        state=GenerationState.COMPLETE,
    )
    relative = f"projects/unsorted/images/{row.id}.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    state.db.set_generation_file(row.id, relative)
    state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )
    return row


def test_the_create_screen_renders_with_a_model_picker(pages):
    client, _ = pages

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'name="prompt"' in response.text
    assert "openai/gpt-image-2" in response.text


def test_the_library_screen_lists_completed_generations(pages):
    client, state = pages
    row = _completed_generation(state)

    response = client.get("/library")

    assert response.status_code == 200
    assert row.id in response.text
    assert "neon city street" in response.text


def test_the_detail_screen_shows_metadata_and_the_media_url(pages):
    client, state = pages
    row = _completed_generation(state)

    response = client.get(f"/library/{row.id}")

    assert response.status_code == 200
    assert f"/media/projects/unsorted/images/{row.id}.png" in response.text
    assert "openai/gpt-image-2" in response.text


def test_an_unknown_detail_id_is_404(pages):
    client, _ = pages

    assert client.get("/library/000000000000").status_code == 404


def test_the_jobs_screen_subscribes_to_the_event_stream(pages):
    client, _ = pages

    response = client.get("/jobs")

    assert response.status_code == 200
    assert "/events/jobs" in response.text


def test_the_settings_screen_shows_only_a_masked_key(pages):
    client, state = pages
    secret = "sk-or-v1-abcdef0123456789"
    state.db.set_setting("openrouter_api_key", secret)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "...6789" in response.text
    assert secret not in response.text


def test_no_template_references_an_external_host():
    # The UI must work on an offline LAN, so every asset is vendored.
    pattern = re.compile(r"""(?:src|href)\s*=\s*["'](https?:)?//""")

    offenders = [
        path.name
        for path in TEMPLATES_DIR.rglob("*.html")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_vendored_stylesheet_is_served(pages):
    client, _ = pages

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_pages.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.web.pages'`.

- [ ] **Step 3: Implement**

Vendor HTMX once, at development time, and commit the file (this is a one-off
developer action, not something any test performs):

```bash
mkdir -p src/higgshole/web/static/vendor
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js \
  -o src/higgshole/web/static/vendor/htmx.min.js
curl -fsSL https://unpkg.com/htmx-ext-sse@2.2.2/sse.js \
  -o src/higgshole/web/static/vendor/htmx-sse.js
```

Create `src/higgshole/web/static/app.css`:

```css
:root {
  --bg: #14161a;
  --panel: #1c1f26;
  --line: #2c313b;
  --text: #e6e8ec;
  --muted: #9aa3b2;
  --accent: #6ea8fe;
  --warn: #e0b354;
  --bad: #e06c75;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 system-ui, sans-serif;
}

header.topbar {
  display: flex;
  gap: 1.5rem;
  align-items: baseline;
  padding: 0.75rem 1.25rem;
  border-bottom: 1px solid var(--line);
}

header.topbar a { color: var(--muted); text-decoration: none; }
header.topbar a.active, header.topbar a:hover { color: var(--text); }
header.topbar .budget { margin-left: auto; color: var(--muted); font-size: 0.9em; }

main { padding: 1.25rem; max-width: 1200px; }

.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}

label { display: block; margin: 0.75rem 0 0.25rem; color: var(--muted); }

input, select, textarea, button {
  font: inherit;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.45rem 0.6rem;
  width: 100%;
}

button {
  width: auto;
  cursor: pointer;
  border-color: var(--accent);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 0.75rem;
}

.card { position: relative; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.card img { width: 100%; display: block; aspect-ratio: 1; object-fit: cover; background: #000; }
.card .caption { padding: 0.4rem 0.5rem; font-size: 0.85em; color: var(--muted); }
.badge {
  position: absolute; top: 0.4rem; right: 0.4rem;
  background: rgba(0, 0, 0, 0.7); border-radius: 4px;
  padding: 0.1rem 0.35rem; font-size: 0.75em;
}

.issue-hard { color: var(--bad); }
.issue-advisory { color: var(--warn); }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, monospace; }

table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--line); }
```

Create `src/higgshole/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{% block title %}HiggsHole{% endblock %}</title>
    <link rel="stylesheet" href="/static/app.css" />
    <script src="/static/vendor/htmx.min.js" defer></script>
    <script src="/static/vendor/htmx-sse.js" defer></script>
  </head>
  <body>
    <header class="topbar">
      <strong>HiggsHole</strong>
      <a href="/" class="{{ 'active' if screen == 'create' }}">Create</a>
      <a href="/library" class="{{ 'active' if screen == 'library' }}">Library</a>
      <a href="/jobs" class="{{ 'active' if screen == 'jobs' }}">Jobs</a>
      <a href="/settings" class="{{ 'active' if screen == 'settings' }}">Settings</a>
      <span class="budget">
        {% if budget.provider_available %}
          {{ budget.provider_remaining_usd or "—" }} USD remaining
        {% else %}
          local only
        {% endif %}
        {% if budget.cap_usd %}
          · cap {{ budget.cap_usd }} · spent {{ "≥" if budget.is_lower_bound }}{{ budget.spent_today_usd }}
        {% endif %}
      </span>
    </header>
    <main>{% block content %}{% endblock %}</main>
  </body>
</html>
```

Create `src/higgshole/web/templates/create.html`:

```html
{% extends "base.html" %}
{% block title %}Create · HiggsHole{% endblock %}
{% block content %}
<form class="panel" id="create-form">
  <label for="kind">Type</label>
  <select id="kind" name="kind"
          hx-get="/partials/model-controls" hx-target="#model-controls"
          hx-include="#create-form">
    <option value="image">Image</option>
    <option value="video">Video</option>
  </select>

  <label for="model">Model</label>
  <select id="model" name="model"
          hx-get="/partials/model-controls" hx-target="#model-controls"
          hx-include="#create-form">
    {% for model in favourites %}
      <option value="{{ model.id }}">★ {{ model.name }}</option>
    {% endfor %}
    {% for model in models %}
      <option value="{{ model.id }}">{{ model.name }} ({{ model.kind }})</option>
    {% endfor %}
  </select>

  <label for="project">Project</label>
  <select id="project" name="project">
    {% for project in projects %}
      <option value="{{ project.slug }}">{{ project.name }}</option>
    {% endfor %}
  </select>

  <label for="prompt">Prompt</label>
  <textarea id="prompt" name="prompt" rows="4"
            placeholder="Passed to the provider verbatim"></textarea>

  <div id="model-controls">{% include "partials/model_controls.html" %}</div>

  <div id="estimate" hx-get="/partials/estimate" hx-trigger="change from:#create-form"
       hx-include="#create-form">
    {% include "partials/estimate.html" %}
  </div>

  <p><button type="submit">Generate</button></p>
</form>
{% endblock %}
```

Create `src/higgshole/web/templates/library.html`:

```html
{% extends "base.html" %}
{% block title %}Library · HiggsHole{% endblock %}
{% block content %}
<form class="panel" id="library-filters"
      hx-get="/partials/library-grid" hx-target="#library-grid" hx-trigger="change">
  <label for="project">Project</label>
  <select id="project" name="project">
    <option value="">All projects</option>
    {% for project in projects %}
      <option value="{{ project.slug }}" {{ 'selected' if project.slug == selected_project }}>
        {{ project.name }} ({{ project.item_count }})
      </option>
    {% endfor %}
  </select>

  <label for="kind">Type</label>
  <select id="kind" name="kind">
    <option value="">Everything</option>
    <option value="image">Images</option>
    <option value="video">Videos</option>
  </select>

  <label for="model">Model</label>
  <input id="model" name="model" placeholder="Any model" />
</form>

<div id="library-grid">{% include "partials/library_grid.html" %}</div>
{% endblock %}
```

Create `src/higgshole/web/templates/detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ item.id }} · HiggsHole{% endblock %}
{% block content %}
<div class="panel">
  {% if item.asset %}
    {% if item.kind == "video" %}
      <video controls preload="metadata" style="max-width:100%"
             {% if item.poster_url %}poster="{{ item.poster_url }}"{% endif %}
             src="{{ item.asset.url }}"></video>
    {% else %}
      <img src="{{ item.asset.url }}" alt="{{ item.prompt }}" style="max-width:100%" />
    {% endif %}
  {% else %}
    <p class="muted">No media on disk for this generation ({{ item.state }}).</p>
  {% endif %}
</div>

<div class="panel">
  <table>
    <tr><th>Prompt</th><td>{{ item.prompt }}</td></tr>
    <tr><th>Model</th><td class="mono">{{ item.model }}</td></tr>
    <tr><th>State</th><td>{{ item.state }}</td></tr>
    <tr><th>Project</th><td>{{ item.project_slug }}</td></tr>
    <tr>
      <th>Cost</th>
      <td>{{ item.cost_usd if item.cost_known else "not reported by the provider" }}</td>
    </tr>
    <tr><th>Created</th><td>{{ item.created_at }}</td></tr>
    {% if item.asset %}
      <tr><th>Local path</th><td class="mono">{{ item.asset.local_path }}</td></tr>
    {% endif %}
    {% if item.error_reason %}
      <tr><th>Error</th><td class="issue-hard">{{ item.error_reason }} — {{ item.error_detail }}</td></tr>
    {% endif %}
  </table>
  {% for key, value in item.params.items() %}
    <p class="muted mono">{{ key }} = {{ value }}</p>
  {% endfor %}
</div>

<div class="panel">
  <h3>Lineage</h3>
  {% if item.inputs %}
    <div class="grid">
      {% for input in item.inputs %}
        <div class="card">
          {% if input.thumb_url %}<img src="{{ input.thumb_url }}" alt="input" />{% endif %}
          <div class="caption">{{ input.role }} #{{ input.position }}</div>
        </div>
      {% endfor %}
    </div>
  {% else %}
    <p class="muted">This generation used no inputs.</p>
  {% endif %}
</div>
{% endblock %}
```

Create `src/higgshole/web/templates/jobs.html`:

```html
{% extends "base.html" %}
{% block title %}Jobs · HiggsHole{% endblock %}
{% block content %}
<div class="panel" hx-ext="sse" sse-connect="/events/jobs">
  <h3>In flight</h3>
  <table id="job-table" sse-swap="job" hx-swap="afterbegin">
    <tr><th>Generation</th><th>Kind</th><th>State</th><th>Model</th></tr>
    {% for job in jobs %}
      {% with item = job %}{% include "partials/job_row.html" %}{% endwith %}
    {% endfor %}
  </table>
  {% if not jobs %}<p class="muted">Nothing is running.</p>{% endif %}
</div>
{% endblock %}
```

Create `src/higgshole/web/templates/settings.html`:

```html
{% extends "base.html" %}
{% block title %}Settings · HiggsHole{% endblock %}
{% block content %}
<form class="panel" id="settings-form">
  <h3>API keys</h3>
  <p class="muted">
    Keys are write-only. After saving, only the last four characters are ever
    shown again.
  </p>

  <label for="openrouter_api_key">Shared key ({{ settings.openrouter_api_key_masked or "not set" }})</label>
  <input id="openrouter_api_key" name="openrouter_api_key" type="password"
         pattern="sk-or-v1-.+" placeholder="sk-or-v1-…"
         title="An OpenRouter key begins sk-or-v1-" />

  <label for="openrouter_api_key_image">Image key ({{ settings.openrouter_api_key_image_masked or "falls back to shared" }})</label>
  <input id="openrouter_api_key_image" name="openrouter_api_key_image" type="password"
         pattern="sk-or-v1-.+" placeholder="sk-or-v1-…" />

  <label for="openrouter_api_key_video">Video key ({{ settings.openrouter_api_key_video_masked or "falls back to shared" }})</label>
  <input id="openrouter_api_key_video" name="openrouter_api_key_video" type="password"
         pattern="sk-or-v1-.+" placeholder="sk-or-v1-…" />

  <label for="daily_cap_usd">Local daily cap (USD, blank for none)</label>
  <input id="daily_cap_usd" name="daily_cap_usd" value="{{ settings.daily_cap_usd or '' }}" />

  <p><button type="submit">Save</button></p>
</form>

<div class="panel">
  <h3>Catalogue</h3>
  <table>
    <tr><th>Images fetched</th><td>{{ settings.catalog.image_fetched_at or "never" }}</td></tr>
    <tr><th>Videos fetched</th><td>{{ settings.catalog.video_fetched_at or "never" }}</td></tr>
    <tr><th>Stale</th><td>{{ "yes" if settings.catalog.is_stale else "no" }}</td></tr>
    {% if settings.catalog.last_error %}
      <tr><th>Last error</th><td class="issue-advisory">{{ settings.catalog.last_error }}</td></tr>
    {% endif %}
  </table>
  <p>
    <button hx-post="/api/settings/catalog/refresh" hx-swap="none">Refresh catalogue</button>
    <button hx-post="/api/settings/rescan" hx-swap="none">Rescan media from disk</button>
  </p>
</div>

<div class="panel">
  <h3>Environment</h3>
  <table>
    <tr><th>ffmpeg and ffprobe</th><td>{{ "available" if settings.ffmpeg_available else "MISSING — video will fail" }}</td></tr>
    <tr><th>Reference transport</th><td class="mono">{{ settings.reference_transport }}</td></tr>
    <tr><th>Video reference slots</th><td>{{ "offered" if settings.video_references_supported else "disabled for this transport" }}</td></tr>
  </table>
  {% if resume_report %}
    <p class="muted">
      Last restart reattached {{ resume_report.reattached|length }} job(s),
      timed out {{ resume_report.timed_out|length }}, and could not recover
      {{ resume_report.orphaned|length }}.
    </p>
  {% endif %}
</div>
{% endblock %}
```

Create `src/higgshole/web/pages.py`:

```python
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

from higgshole.store.db import TERMINAL_STATES, GenerationKind, GenerationState, MediaFilter
from higgshole.web.api import (
    _budget_out,
    _settings_out,
    current_key_status,
    error_response,
    generation_out,
    list_models,
    list_projects,
)
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
    return templates.TemplateResponse("create.html", context)


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
        "items": [generation_out(state, row) for row in state.db.list_generations(filters)],
    }
    return templates.TemplateResponse("library.html", context)


@router.get("/library/{gen_id}", response_class=HTMLResponse)
async def detail_screen(
    request: Request, gen_id: str, state: AppState = Depends(get_state)
) -> HTMLResponse:
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")

    context = await _chrome(request, state, "library")
    context |= {"item": generation_out(state, row)}
    return templates.TemplateResponse("detail.html", context)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_screen(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    in_flight = [s for s in GenerationState if s not in TERMINAL_STATES]
    rows = state.db.list_generations_in_states(in_flight)
    context = await _chrome(request, state, "jobs")
    context |= {"jobs": [generation_out(state, row) for row in rows]}
    return templates.TemplateResponse("jobs.html", context)


@router.get("/settings", response_class=HTMLResponse)
async def settings_screen(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    context = await _chrome(request, state, "settings")
    context |= {
        "settings": _settings_out(state),
        "resume_report": state.resume_report,
    }
    return templates.TemplateResponse("settings.html", context)
```

In `src/higgshole/web/app.py`, mount the static files and include the pages
router. Replace the router registration at the bottom of `create_app` with:

```python
    from fastapi.staticfiles import StaticFiles

    from higgshole.web import api, pages  # here, to avoid a circular import

    app.include_router(api.router)
    app.include_router(sse.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=str(pages.STATIC_DIR)), name="static")
    return app
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_pages.py -v`

Expected: PASS — `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/pages.py src/higgshole/web/templates/ \
        src/higgshole/web/static/ src/higgshole/web/app.py tests/web/test_pages.py
git commit -m "feat: add the five HTMX screens with vendored assets"
```

---

## Task 9: Capability-derived partials

**Files:**
- Modify: `src/higgshole/web/pages.py`
- Create: `src/higgshole/web/templates/partials/model_controls.html`, `library_grid.html`, `job_row.html`, `estimate.html`
- Test: `tests/web/test_partials.py`

**Interfaces:**
- Consumes: `list_models`, `estimate` (the API route function), `GenerateImageIn`, `GenerateVideoIn`, `generation_out`, `video_references_supported`.
- Produces: routes `GET /partials/model-controls`, `GET /partials/library-grid`, `GET /partials/job-row`, `GET /partials/estimate`.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_partials.py`:

```python
import pytest
from starlette.testclient import TestClient

from higgshole.store.db import GenerationKind, GenerationState
from tests.web.fakes import build_test_state


@pytest.fixture
def pages(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_controls_offer_only_the_durations_the_model_declares(pages):
    # Spec section 6.1: controls are rendered from discovered capabilities;
    # an option the model does not support is never offered.
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro"},
    ).text

    assert 'value="5"' in html
    assert 'value="10"' in html
    assert 'value="4"' not in html
    assert 'value="8"' not in html


def test_controls_offer_only_the_resolutions_the_model_declares(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "openai/sora-2-pro"},
    ).text

    assert 'value="720p"' in html
    assert 'value="1080p"' in html
    assert 'value="480p"' not in html


def test_a_text_only_model_is_offered_no_frame_slots(pages):
    # openai/sora-2-pro accepts no frame images at all (spec section 2.7).
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "openai/sora-2-pro"},
    ).text

    assert "first_frame_asset_id" not in html
    assert "last_frame_asset_id" not in html


def test_a_first_and_last_frame_model_is_offered_both_slots(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro"},
    ).text

    assert "first_frame_asset_id" in html
    assert "last_frame_asset_id" in html


def test_reference_slots_appear_only_in_the_quantity_the_model_accepts(pages):
    client, _ = pages

    html = client.get(
        "/partials/model-controls",
        params={"kind": "image", "model": "recraft/recraft-v4.1"},
    ).text

    assert html.count('name="input_reference_asset_ids"') == 1


def test_an_estimate_partial_shows_a_reason_rather_than_a_number(pages, monkeypatch):
    from higgshole.budget.estimator import Estimate, EstimateUnavailable
    from higgshole.web import api as api_module

    monkeypatch.setattr(
        api_module,
        "estimate_video_cost",
        lambda *a, **k: Estimate(
            amount=None,
            reason=EstimateUnavailable.VIDEO_TOKEN_PRICED,
            detail="priced in video tokens with no published conversion table",
        ),
    )
    client, _ = pages

    html = client.get(
        "/partials/estimate",
        params={"kind": "video", "model": "kwaivgi/kling-v3.0-pro", "prompt": "x"},
    ).text

    assert "no published conversion table" in html
    assert "$" not in html


def test_an_estimate_partial_shows_the_amount_when_it_is_exact(pages):
    client, _ = pages

    html = client.get(
        "/partials/estimate",
        params={"kind": "image", "model": "openai/gpt-image-2", "prompt": "x"},
    ).text

    assert "USD" in html


def test_the_library_grid_partial_is_a_fragment_not_a_document(pages):
    client, state = pages
    project = state.db.get_project_by_slug("unsorted")
    state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="fragment probe",
        params={},
        state=GenerationState.COMPLETE,
    )

    html = client.get("/partials/library-grid").text

    assert "<html" not in html.lower()
    assert "fragment probe" in html


def test_the_job_row_partial_renders_the_current_state(pages):
    client, state = pages
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="row probe",
        params={},
        state=GenerationState.RUNNING,
    )

    html = client.get("/partials/job-row", params={"gen_id": row.id}).text

    assert "RUNNING" in html
    assert row.id in html
    assert "<html" not in html.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_partials.py -v`

Expected: FAIL — `assert 404 == 200` / `Not Found`; none of the `/partials/*` routes exist.

- [ ] **Step 3: Implement**

Create `src/higgshole/web/templates/partials/model_controls.html`:

```html
{% if model %}
  <fieldset class="panel">
    <legend class="muted">{{ model.name }}</legend>

    {% if model.supported_aspect_ratios %}
      <label for="aspect_ratio">Aspect ratio</label>
      <select id="aspect_ratio" name="aspect_ratio">
        {% for value in model.supported_aspect_ratios %}
          <option value="{{ value }}">{{ value }}</option>
        {% endfor %}
      </select>
    {% endif %}

    {% if model.supported_resolutions %}
      <label for="resolution">Resolution</label>
      <select id="resolution" name="resolution">
        {% for value in model.supported_resolutions %}
          <option value="{{ value }}">{{ value }}</option>
        {% endfor %}
      </select>
    {% endif %}

    {% if model.supported_durations %}
      <label for="duration">Duration (seconds)</label>
      <select id="duration" name="duration">
        {% for value in model.supported_durations %}
          <option value="{{ value }}">{{ value }}</option>
        {% endfor %}
      </select>
    {% endif %}

    {% if model.quality_values %}
      <label for="quality">Quality</label>
      <select id="quality" name="quality">
        {% for value in model.quality_values %}
          <option value="{{ value }}">{{ value }}</option>
        {% endfor %}
      </select>
    {% endif %}

    {% if model.generate_audio %}
      <label for="generate_audio">Generate audio</label>
      <select id="generate_audio" name="generate_audio">
        <option value="false">No</option>
        <option value="true">Yes</option>
      </select>
    {% endif %}

    {% if model.seed %}
      <label for="seed">Seed</label>
      <input id="seed" name="seed" type="number" placeholder="leave blank for random" />
    {% endif %}

    {% if model.kind == "video" %}
      {% if model.supported_frame_images and video_references_supported %}
        {% if "first_frame" in model.supported_frame_images %}
          <label for="first_frame_asset_id">First frame (asset id)</label>
          <input id="first_frame_asset_id" name="first_frame_asset_id" />
        {% endif %}
        {% if "last_frame" in model.supported_frame_images %}
          <label for="last_frame_asset_id">Last frame (asset id)</label>
          <input id="last_frame_asset_id" name="last_frame_asset_id" />
        {% endif %}
      {% elif model.supported_frame_images and not video_references_supported %}
        <p class="issue-advisory">
          Reference images are unavailable: the configured transport cannot
          deliver a local file to the provider.
        </p>
      {% else %}
        <p class="muted">{{ model.name }} is text-to-video only.</p>
      {% endif %}
    {% else %}
      {% for slot in range(model.max_input_references) %}
        <label for="input_reference_{{ slot }}">Reference image {{ slot + 1 }} (asset id)</label>
        <input id="input_reference_{{ slot }}" name="input_reference_asset_ids" />
      {% endfor %}
    {% endif %}
  </fieldset>
{% else %}
  <p class="muted">Choose a model to see its options.</p>
{% endif %}
```

Create `src/higgshole/web/templates/partials/library_grid.html`:

```html
<div class="grid">
  {% for item in items %}
    <a class="card" href="/library/{{ item.id }}">
      {% if item.thumb_url %}
        <img src="{{ item.thumb_url }}" alt="{{ item.prompt }}" loading="lazy" />
      {% endif %}
      {% if item.kind == "video" and item.asset and item.asset.duration_s %}
        <span class="badge">{{ "%.0f"|format(item.asset.duration_s) }}s</span>
      {% endif %}
      <span class="caption">{{ item.prompt[:60] }}</span>
      <span class="caption mono">{{ item.id }}</span>
    </a>
  {% else %}
    <p class="muted">Nothing here yet.</p>
  {% endfor %}
</div>
```

Create `src/higgshole/web/templates/partials/job_row.html`:

```html
<tr id="job-{{ item.id }}">
  <td class="mono"><a href="/library/{{ item.id }}">{{ item.id }}</a></td>
  <td>{{ item.kind }}</td>
  <td>
    {{ item.state }}
    {% if item.error_reason %}
      <span class="issue-hard">({{ item.error_reason }})</span>
    {% endif %}
  </td>
  <td class="mono">{{ item.model }}</td>
</tr>
```

Create `src/higgshole/web/templates/partials/estimate.html`:

```html
{% if estimate is none %}
  <p class="muted">Choose a model to see an estimate.</p>
{% elif estimate.amount_usd %}
  <p>Estimated cost: <strong>{{ estimate.amount_usd }} USD</strong></p>
{% else %}
  <p class="issue-advisory">
    No estimate available ({{ estimate.estimate_unavailable }}):
    {{ estimate.detail }}
  </p>
{% endif %}
```

Append to `src/higgshole/web/pages.py`:

```python
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
        "partials/model_controls.html",
        {
            "request": request,
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
        "partials/estimate.html", {"request": request, "estimate": result}
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
        "partials/library_grid.html", {"request": request, "items": items}
    )


@router.get("/partials/job-row", response_class=HTMLResponse)
async def job_row_partial(
    request: Request, gen_id: str = Query(...), state: AppState = Depends(get_state)
) -> HTMLResponse:
    row = state.db.get_generation(gen_id)
    if row is None:
        raise error_response(404, "generation_not_found", f"No generation {gen_id!r}.")
    return templates.TemplateResponse(
        "partials/job_row.html",
        {"request": request, "item": generation_out(state, row)},
    )
```

Extend the `higgshole.web.api` import at the top of `pages.py` so the estimate
route function is reachable under an unambiguous name:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/web/test_partials.py -v`

Expected: PASS — `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/web/pages.py src/higgshole/web/templates/partials/ \
        tests/web/test_partials.py
git commit -m "feat: render generation controls from the capability catalogue"
```

---

## Task 10: The assembled application

**Files:**
- Modify: `pyproject.toml` (register the console script)
- Test: `tests/web/test_integration.py`

**Interfaces:**
- Consumes: everything above.
- Produces: the `higgshole` console script pointing at `higgshole.web.app:run`. No new Python symbols.

- [ ] **Step 1: Write the failing test**

Create `tests/web/test_integration.py`:

```python
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def app_client(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_the_media_mount_and_the_api_share_one_application(app_client):
    client, state = app_client
    relative = "projects/unsorted/images/probe.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)

    assert client.get("/api/projects").status_code == 200
    assert client.get(f"/media/{relative}").status_code == 200
    assert client.get("/").status_code == 200


def test_a_completed_generation_is_browsable_and_playable(app_client):
    client, state = app_client
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="end to end",
        params={},
        state=GenerationState.COMPLETE,
    )
    relative = f"projects/unsorted/images/{row.id}.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    state.db.set_generation_file(row.id, relative)
    state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )

    listed = client.get("/api/media").json()
    url = listed["items"][0]["asset"]["url"]

    assert client.get(url).content == PNG_1X1
    assert row.id in client.get("/library").text


def test_the_event_stream_advertises_the_sse_content_type(app_client):
    client, _ = app_client

    with client.stream("GET", "/events/jobs") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


def test_every_documented_route_is_present_in_the_schema(app_client):
    client, _ = app_client

    paths = set(client.get("/api/openapi.json").json()["paths"])

    assert {
        "/api/models",
        "/api/estimate",
        "/api/generate/image",
        "/api/generate/video",
        "/api/jobs",
        "/api/jobs/{gen_id}",
        "/api/uploads",
        "/api/media",
        "/api/media/{gen_id}",
        "/api/projects",
        "/api/budget",
        "/api/settings",
        "/api/settings/catalog/refresh",
        "/api/settings/rescan",
    } <= paths


def test_the_console_script_starts_the_single_worker_entrypoint():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["scripts"]["higgshole"] == "higgshole.web.app:run"


def test_no_source_file_references_an_external_asset_host():
    # The console must work on an offline LAN, so nothing may be fetched at
    # page load from anywhere but this service.
    offenders = []
    for path in Path("src/higgshole").rglob("*"):
        if path.suffix not in {".html", ".py", ".css"} or "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "cdn." in text or "unpkg.com" in text or "jsdelivr" in text:
            offenders.append(str(path))

    assert offenders == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/web/test_integration.py -v`

Expected: FAIL — `KeyError: 'scripts'` in `test_the_console_script_starts_the_single_worker_entrypoint`; the other tests pass already.

- [ ] **Step 3: Implement**

Add the console script to `pyproject.toml`, immediately after the `[project]`
table's `dependencies`:

```toml
[project.scripts]
higgshole = "higgshole.web.app:run"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv sync --extra dev && uv run pytest tests/web/ -v`

Expected: PASS — `6 passed` for this file, and the whole `tests/web/` package green.

- [ ] **Step 5: Run the full suite and lint, then commit**

Run: `uv run pytest -q && uv run ruff check .`

Expected: every test passes, `All checks passed!`

```bash
git add pyproject.toml uv.lock tests/web/test_integration.py
git commit -m "feat: register the single-worker console entrypoint"
```

---

## Definition of done

- [ ] `uv run pytest -q` passes with no network access and no spend
- [ ] `uv run ruff check .` is clean
- [ ] A range request returns 206 with a correct `Content-Range`; `bytes=-500` works; an unsatisfiable range returns 416
- [ ] The regression test adds `GZipMiddleware` to the parent application and proves a 206 media response still carries **no** `Content-Encoding`, and that the middleware really is active
- [ ] Encoded and absolute path traversal attempts return 404 and leak nothing
- [ ] Every error body carries `error` and `message` at the **top level**, never nested under `detail`
- [ ] An API key or daily cap saved through `PUT /api/settings` takes effect without a restart, and `create_app` opens exactly one `Database`
- [ ] An API key saved through `PUT /api/settings` is never returned in full by any endpoint or template — only `mask_key` output
- [ ] A key that does not begin `sk-or-v1-` is rejected before it is stored
- [ ] Generation controls are rendered from the discovered catalogue; a text-to-video model is offered no frame slots and a one-reference image model is offered exactly one slot
- [ ] An unavailable cost estimate renders its machine-readable reason, never a number
- [ ] Every monetary field in every response is a string or `null`
- [ ] `web/app.py` registers no middleware, and `run()` starts exactly one uvicorn worker
- [ ] All five screens and four partials render; no template or source file references an external host
- [ ] `GET /api/openapi.json` lists every route `mcp_server.py` will call
- [ ] No committed file contains a personal name, employer name, machine-specific path, or API key

---

## Contract additions

The frozen contract did not name the following, which Plan 4 needs. Each is
added in the most consistent style available and is additive — nothing frozen
changed shape.

| Symbol | Module | Why |
|---|---|---|
| `HiggsHoleApp(FastAPI)` with `media_app` and `MEDIA_PREFIXES` | `web/app.py` | The contract says media is "structurally exempt from middleware" because it is mounted. In Starlette that is not true — `add_middleware` wraps the whole router, mounts included — so the exemption is implemented by dispatching the media prefixes ahead of `Starlette.middleware_stack`. This is what makes the required regression test meaningful rather than vacuous. |
| `build_app_state(settings, db=None) -> AppState` | `web/app.py` | The real object graph must be assembled somewhere; factoring it out of `lifespan` lets tests supply their own `AppState` by setting `app.state.higgshole` before startup. |
| `AppState.client_factory`, `AppState.key_status_cached` | `web/app.py` | `GET /api/budget` needs a `KeyStatus`, and `BudgetGate.status()` takes one as an argument rather than fetching it. The factory and the 60-second cache from spec §3.2 have to live on the state. |
| The `HTTPException` handler registered in `create_app` | `web/app.py` | The contract's error body is `{"error", "message", "issues"}` at the top level, but FastAPI's default handler nests any `HTTPException.detail` under `"detail"`. The handler flattens dict details so the browser and `mcp_server.py` read one shape, and gives plain-string exceptions the same two fields. |
| `resolve_api_key(db, settings, kind)`, `resolve_daily_cap(db, settings)`, `BudgetGate.set_daily_cap` | `web/app.py`, `budget/gate.py` | Spec §8 allows keys and the cap to be set through the UI, which only works if the settings table overlays the environment and the key is resolved per call rather than at startup. |
| `current_key_status(state)`, `KEY_STATUS_TTL_SECONDS` | `web/api.py` | Implements the "cached for 60 seconds" rule in spec §3.2. |
| `generation_out(state, row) -> GenerationOut` | `web/api.py` | The row-to-view-model conversion used by the API, the pages and the partials, so all three cannot diverge. |
| `JOB_POLL_INTERVAL_S`, `MAX_UPLOAD_BYTES`, `FAVOURITES_SETTING` | `web/api.py` | Named constants behind the long-poll, the `upload_too_large` code and the `favourite_models` settings key, all of which the contract requires but does not parameterise. |
| `rescan_library(state) -> RescanOut` | `web/api.py` | `POST /api/settings/rescan` needs an implementation; the contract specifies only its response model. |
| `event_stream(bus, *, keepalive_seconds)` and `EventBus.listener_count` | `web/sse.py` | Makes SSE framing and listener bookkeeping testable without an application or a socket. |
| `TEMPLATES_DIR`, `STATIC_DIR` | `web/pages.py` | Needed to configure `Jinja2Templates` and the `/static` mount, and to let a test assert that no template references an external host. |
| `serve_media` / `serve_thumb` accept `HEAD` as well as `GET` | `web/media.py` | Browsers issue `HEAD` before seeking a video; refusing it would break playback for no benefit. |
