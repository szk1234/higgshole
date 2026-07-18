# HiggsHole Plan 3 — Job Engine

> **How to execute this plan:** work through it strictly task by task, in order.
> Each task is self-contained and ends with a passing test suite and a commit,
> so it is a natural review checkpoint — do not start the next task until the
> current one is green. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Every task follows the same cycle: write a failing test, run it to confirm it
> fails for the reason you expect, write the minimal implementation, confirm it
> passes, commit. Do not write implementation before its test.

**Goal:** Build the two generation state machines, the reference transport that feeds them, and the boot-time poller reattachment that makes an in-flight video job survive a restart.

**Architecture:** `jobs/` sits above `orclient/`, `store/`, `budget/` and `catalog/` and below `web/`. `JobRunner` holds the plumbing shared by both machines — persistence, budget settlement, sidecar writing, thumbnailing — while `ImageJobRunner` (synchronous) and `VideoJobRunner` (submit-then-poll) implement the two different shapes. Every provider call goes through an injected `client_factory` and every wait goes through an injected `Clock`, so the whole engine is exercised offline, instantly, and for free.

**Tech Stack:** Python 3.12+, `uv`, `asyncio`, `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`).

**Source specification:** docs/specs/2026-07-18-higgshole-design.md

**Depends on:** Plan 1 (config, `orclient`, `catalog.validation`), Plan 2 (`store`, `budget`, `catalog.cache`)

## Global Constraints

- **Python 3.12+**, `uv` for dependency management, pytest with `asyncio_mode = "auto"`.
- **Public repository.** No committed file may contain a personal name, an employer name, a machine-specific absolute path, or an API key.
- **No test may make a real network request or cost money.** The autouse `_forbid_real_network` fixture from Plan 1 Task 10 is inherited. `tests/jobs/conftest.py` adds fixtures only — it must never redefine that guard.
- **Every provider interaction in tests goes through `FakeOpenRouterClient`.** The real `OpenRouterClient` is never constructed in this plan's tests.
- **Time is injected.** `Clock` is a protocol; tests use `FakeClock`, so a 30-minute timeout test finishes in microseconds and no test ever calls `asyncio.sleep` with a real duration.
- **Never fabricate a cost.** `Decimal | None` everywhere; `None` for unknown; `0` only for a genuine zero.
- **Terminal provider statuses are exactly** `completed`, `failed`, `cancelled`, `expired`. Any *unrecognised* status is non-terminal — keep polling (spec §2.4).
- **Ordering is fixed:** local validation → budget gate → dispatch (spec §4.3).
- **The provider job ID is committed to SQLite before polling begins** (spec §4.3 durability rule).
- **Submission is never blindly retried.** Only HTTP 429 before dispatch, and idempotent GETs (poll, download), are retried (spec §4.4).
- **Reservations are reversed on every terminal state**, with the single documented exception of a completed job reporting no cost (spec §3.4).
- **`jobs/` never imports `web/`.** Task 9 asserts this.
- Commit after every task. Conventional commit prefixes (`feat:`, `test:`, `chore:`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/higgshole/jobs/__init__.py` | Public re-exports for the job engine |
| `src/higgshole/jobs/references.py` | Turn stored assets into provider-ready reference URLs (spec §2.8) |
| `src/higgshole/jobs/events.py` | `JobEvent` and the `EventPublisher` protocol the runners publish to |
| `src/higgshole/jobs/clock.py` | Injectable monotonic clock and sleep |
| `src/higgshole/jobs/runner.py` | `JobRunner`, `ImageJobRunner`, `VideoJobRunner`, `map_provider_status` |
| `src/higgshole/jobs/resume.py` | Boot-time poller reattachment (spec §4.3) |
| `tests/jobs/__init__.py` | Test package marker |
| `tests/jobs/fakes.py` | `FakeOpenRouterClient`, `FakeClock`, `FakeCatalog`, `RecordingPublisher`, `Harness` |
| `tests/jobs/conftest.py` | Fixtures only — never redefines the network guard |
| `tests/jobs/test_references.py` | Reference transport |
| `tests/jobs/test_events.py` | Event payload and clock |
| `tests/jobs/test_status_mapping.py` | Provider status mapping and retry policy |
| `tests/jobs/test_runner_base.py` | Shared runner plumbing |
| `tests/jobs/test_image_runner.py` | The synchronous image machine |
| `tests/jobs/test_video_submit.py` | Video submit and poller attachment |
| `tests/jobs/test_video_poll.py` | Polling, timeout, download, settlement |
| `tests/jobs/test_resume.py` | Restart reattachment |
| `tests/jobs/test_invariants.py` | Cap, in-flight ceiling, ledger arithmetic, layering |

---

## Task 1: Reference transport

**Files:**
- Create: `src/higgshole/jobs/__init__.py`
- Create: `src/higgshole/jobs/references.py`
- Create: `tests/jobs/__init__.py`
- Test: `tests/jobs/test_references.py`

**Interfaces:**
- Consumes: `higgshole.store.db.AssetRow`, `higgshole.store.db.InputRole`, `higgshole.store.paths.MediaPaths`, `higgshole.store.paths.PathTraversalError`, `higgshole.store.metadata.mime_for`.
- Produces:
  - `ReferenceTransport(StrEnum)` with `DATA_URI = "data_uri"`
  - `DEFAULT_MAX_DATA_URI_BYTES: int`
  - `encode_data_uri(path: Path, *, mime_type: str | None = None, max_bytes: int = DEFAULT_MAX_DATA_URI_BYTES) -> str`
  - `build_reference(asset: AssetRow, paths: MediaPaths, *, transport: ReferenceTransport) -> str`
  - `build_video_frame_images(inputs: Sequence[tuple[AssetRow, InputRole]], paths: MediaPaths, *, transport: ReferenceTransport) -> list[tuple[str, str]]`
  - `build_input_references(inputs: Sequence[tuple[AssetRow, InputRole]], paths: MediaPaths, *, transport: ReferenceTransport) -> list[str]`
  - `video_references_supported(transport: ReferenceTransport) -> bool`
  - `ReferenceTooLargeError(ValueError)`, `UnsupportedTransportError(ValueError)`

- [ ] **Step 1: Write the failing test**

Create an empty `tests/jobs/__init__.py`, then `tests/jobs/test_references.py`:

```python
import base64

import pytest

from higgshole.jobs.references import (
    ReferenceTooLargeError,
    ReferenceTransport,
    UnsupportedTransportError,
    build_input_references,
    build_reference,
    build_video_frame_images,
    encode_data_uri,
    video_references_supported,
)
from higgshole.store.db import AssetKind, AssetRow, InputRole, utc_now_iso
from higgshole.store.paths import MediaPaths, PathTraversalError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stub-pixels"


def _asset(relative_path: str, *, asset_id: str, mime_type: str = "image/png") -> AssetRow:
    return AssetRow(
        id=asset_id,
        generation_id=None,
        kind=AssetKind.UPLOAD,
        file_path=relative_path,
        mime_type=mime_type,
        bytes=len(PNG_BYTES),
        width=4,
        height=4,
        duration_s=None,
        created_at=utc_now_iso(),
    )


@pytest.fixture
def paths(tmp_path):
    media_paths = MediaPaths(tmp_path / "media")
    media_paths.ensure_project_tree("unsorted")
    return media_paths


def _write_upload(paths, name: str) -> str:
    target = paths.uploads_dir("unsorted") / name
    target.write_bytes(PNG_BYTES)
    return target.relative_to(paths.root).as_posix()


def test_encode_data_uri_produces_a_base64_payload(paths):
    relative = _write_upload(paths, "a.png")

    uri = encode_data_uri(paths.root / relative)

    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_BYTES


def test_encode_data_uri_rejects_an_oversized_file(paths):
    # A multi-megabyte data URI inflates the request body by roughly a third
    # and some providers reject it outright, so the ceiling is enforced here.
    relative = _write_upload(paths, "big.png")

    with pytest.raises(ReferenceTooLargeError):
        encode_data_uri(paths.root / relative, max_bytes=4)


def test_build_reference_resolves_an_asset_inside_the_media_root(paths):
    relative = _write_upload(paths, "ref.png")
    asset = _asset(relative, asset_id="0c118b4e77aa")

    uri = build_reference(asset, paths, transport=ReferenceTransport.DATA_URI)

    assert uri.startswith("data:image/png;base64,")


def test_build_reference_rejects_an_unknown_transport(paths):
    # A public-URL mode is explicitly out of scope (spec 2.8): making local
    # files provider-reachable needs a tunnel and contradicts the
    # trusted-network premise.
    relative = _write_upload(paths, "ref.png")
    asset = _asset(relative, asset_id="0c118b4e77aa")

    with pytest.raises(UnsupportedTransportError):
        build_reference(asset, paths, transport="public_url")


def test_build_video_frame_images_keeps_only_frame_roles(paths):
    first = _asset(_write_upload(paths, "first.png"), asset_id="aaaaaaaaaaaa")
    last = _asset(_write_upload(paths, "last.png"), asset_id="bbbbbbbbbbbb")
    other = _asset(_write_upload(paths, "other.png"), asset_id="cccccccccccc")

    frames = build_video_frame_images(
        [
            (first, InputRole.FIRST_FRAME),
            (other, InputRole.INPUT_REFERENCE),
            (last, InputRole.LAST_FRAME),
        ],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert [frame_type for _, frame_type in frames] == ["first_frame", "last_frame"]
    assert all(url.startswith("data:image/png;base64,") for url, _ in frames)


def test_build_input_references_keeps_only_reference_role(paths):
    reference = _asset(_write_upload(paths, "r.png"), asset_id="dddddddddddd")
    frame = _asset(_write_upload(paths, "f.png"), asset_id="eeeeeeeeeeee")

    urls = build_input_references(
        [(frame, InputRole.FIRST_FRAME), (reference, InputRole.INPUT_REFERENCE)],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert len(urls) == 1


def test_build_input_references_preserves_order(paths):
    one = _asset(_write_upload(paths, "1.png"), asset_id="111111111111")
    two = _asset(_write_upload(paths, "2.png"), asset_id="222222222222")
    (paths.root / two.file_path).write_bytes(PNG_BYTES + b"two")

    urls = build_input_references(
        [(one, InputRole.INPUT_REFERENCE), (two, InputRole.INPUT_REFERENCE)],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG_BYTES
    assert base64.b64decode(urls[1].split(",", 1)[1]) == PNG_BYTES + b"two"


def test_video_references_supported_is_true_for_data_uri():
    # Open item 12.1 is unresolved: schema-level acceptance of data URIs by
    # video providers is near-certain but runtime acceptance is untested. If a
    # live test disproves it this returns False and the UI disables the slots.
    assert video_references_supported(ReferenceTransport.DATA_URI) is True


def test_reference_outside_the_media_root_is_refused(paths):
    escaped = _asset("../../etc/passwd", asset_id="ffffffffffff")

    with pytest.raises(PathTraversalError):
        build_reference(escaped, paths, transport=ReferenceTransport.DATA_URI)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_references.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.jobs'`

- [ ] **Step 3: Implement**

Create `src/higgshole/jobs/__init__.py`:

```python
"""Generation state machines and everything that orchestrates a job."""
```

Create `src/higgshole/jobs/references.py`:

```python
"""Reference image transport (spec section 2.8).

A generation may point at assets already in the library or at a file the
operator just uploaded. Providers receive them as strings in a
``ContentPartImage`` envelope, and the strategy for producing that string is a
single configurable transport, ``HIGGSHOLE_REFERENCE_TRANSPORT``.

Only ``data_uri`` is implemented. A public-URL mode is explicitly out of scope:
making local files reachable by an upstream provider requires a tunnel or an
object store, contradicts the trusted-network premise, and would need its own
lifetime and revocation design.

Open item 12.1 is unresolved. Image references via data URI are confirmed
working; whether *video* providers accept them for ``frame_images`` is untested.
``video_references_supported`` is the single switch the UI consults, so if a
live test disproves the assumption exactly one function changes.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from higgshole.store.db import AssetRow, InputRole
from higgshole.store.metadata import mime_for
from higgshole.store.paths import MediaPaths


class ReferenceTransport(StrEnum):
    DATA_URI = "data_uri"


#: Ceiling on one inlined reference. Base64 inflates a payload by roughly a
#: third, so 20 MiB on disk is already ~27 MiB on the wire.
DEFAULT_MAX_DATA_URI_BYTES: int = 20 * 1024 * 1024

#: Roles that occupy a video's first/last frame slots rather than the generic
#: reference list. The provider treats the two fields differently: when both
#: are supplied, frame_images wins (spec section 2.3).
_FRAME_ROLES: frozenset[InputRole] = frozenset(
    {InputRole.FIRST_FRAME, InputRole.LAST_FRAME}
)


class ReferenceTooLargeError(ValueError):
    """A reference exceeded the inlining ceiling."""


class UnsupportedTransportError(ValueError):
    """A transport was requested that this build does not implement."""


def encode_data_uri(
    path: Path,
    *,
    mime_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_DATA_URI_BYTES,
) -> str:
    """Return ``data:<mime>;base64,<payload>`` for a local file."""
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ReferenceTooLargeError(
            f"{path.name} is {len(data)} bytes, above the {max_bytes}-byte "
            "limit for an inlined reference."
        )
    resolved_mime = mime_type or mime_for(path)
    payload = base64.b64encode(data).decode("ascii")
    return f"data:{resolved_mime};base64,{payload}"


def _coerce_transport(transport: ReferenceTransport | str) -> ReferenceTransport:
    """Accept the raw configuration string as well as the enum member."""
    try:
        return ReferenceTransport(str(transport))
    except ValueError as exc:
        raise UnsupportedTransportError(
            f"{transport!r} is not a supported reference transport; "
            f"only {ReferenceTransport.DATA_URI.value} is implemented."
        ) from exc


def build_reference(
    asset: AssetRow,
    paths: MediaPaths,
    *,
    transport: ReferenceTransport,
) -> str:
    """Turn a stored asset into the string orclient sends as a reference URL.

    Containment is re-checked here rather than trusted from the database row,
    so a corrupted or crafted ``file_path`` cannot inline an arbitrary file
    from the host (spec section 7).
    """
    _coerce_transport(transport)
    absolute = paths.resolve_within_root(asset.file_path)
    return encode_data_uri(absolute, mime_type=asset.mime_type)


def build_video_frame_images(
    inputs: Sequence[tuple[AssetRow, InputRole]],
    paths: MediaPaths,
    *,
    transport: ReferenceTransport,
) -> list[tuple[str, str]]:
    """``(url, frame_type)`` pairs for ``OpenRouterClient.submit_video``."""
    return [
        (build_reference(asset, paths, transport=transport), str(role))
        for asset, role in inputs
        if role in _FRAME_ROLES
    ]


def build_input_references(
    inputs: Sequence[tuple[AssetRow, InputRole]],
    paths: MediaPaths,
    *,
    transport: ReferenceTransport,
) -> list[str]:
    """Reference URLs for image generation, in the order supplied."""
    return [
        build_reference(asset, paths, transport=transport)
        for asset, role in inputs
        if role is InputRole.INPUT_REFERENCE
    ]


def video_references_supported(transport: ReferenceTransport) -> bool:
    """Whether video reference slots may be offered at all.

    True for DATA_URI on the strength of schema-level acceptance. Open item
    12.1 has not been resolved; if a live test shows video providers reject
    data URIs this returns False and web/pages.py disables the slots with an
    explanatory message. Image-to-image is unaffected either way.
    """
    return _coerce_transport(transport) is ReferenceTransport.DATA_URI
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_references.py -v`

Expected: PASS — `9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/__init__.py src/higgshole/jobs/references.py tests/jobs/
git commit -m "feat: add data-URI reference transport for generation inputs"
```

---

## Task 2: Job events and the injectable clock

**Files:**
- Create: `src/higgshole/jobs/events.py`
- Create: `src/higgshole/jobs/clock.py`
- Test: `tests/jobs/test_events.py`

**Interfaces:**
- Consumes: `higgshole.store.db.GenerationKind`, `GenerationState`, `ErrorReason`, `utc_now_iso`.
- Produces:
  - `JobEvent(generation_id, kind, state, error_reason, detail, at)` with `to_sse() -> str`
  - `EventPublisher` — a `Protocol` with `publish(event: JobEvent) -> None`
  - `NullEventPublisher` — discards events; the default when no bus is wired
  - `Clock` — a `Protocol` with `monotonic() -> float` and `async sleep(seconds: float) -> None`
  - `RealClock` — the production implementation

`JobEvent` lives here rather than in `web/sse.py` because `jobs/` must never
import `web/` (spec §4.1). Plan 4's `web/sse.py` re-exports it unchanged, so
the contract's §10.4 surface is satisfied without inverting the dependency.

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_events.py`:

```python
import json

from higgshole.jobs.clock import RealClock
from higgshole.jobs.events import JobEvent, NullEventPublisher
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState, utc_now_iso


def _event(**overrides) -> JobEvent:
    fields = {
        "generation_id": "a3f21c9d4e07",
        "kind": GenerationKind.VIDEO,
        "state": GenerationState.RUNNING,
        "error_reason": None,
        "detail": None,
        "at": utc_now_iso(),
    }
    fields.update(overrides)
    return JobEvent(**fields)


def test_job_event_serialises_as_an_sse_frame():
    frame = _event().to_sse()

    assert frame.startswith("event: job\ndata: ")
    assert frame.endswith("\n\n")

    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["generation_id"] == "a3f21c9d4e07"
    assert payload["state"] == "RUNNING"
    assert payload["kind"] == "video"


def test_job_event_carries_a_null_error_reason():
    payload = json.loads(
        _event(
            state=GenerationState.FAILED,
            error_reason=ErrorReason.PROVIDER_EXPIRED,
            detail="retention window elapsed",
        )
        .to_sse()
        .split("data: ", 1)[1]
        .strip()
    )

    assert payload["error_reason"] == "provider_expired"
    assert payload["detail"] == "retention window elapsed"


def test_null_publisher_discards_events():
    publisher = NullEventPublisher()

    publisher.publish(_event())

    assert publisher.publish(_event()) is None


def test_real_clock_reports_monotonic_time():
    clock = RealClock()

    first = clock.monotonic()
    second = clock.monotonic()

    assert second >= first


async def test_real_clock_sleep_returns_promptly():
    # Zero is the only duration a test may ever pass to the real clock; every
    # timing-sensitive test injects FakeClock instead.
    await RealClock().sleep(0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_events.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.jobs.clock'`

- [ ] **Step 3: Implement**

Create `src/higgshole/jobs/events.py`:

```python
"""State-transition events emitted by the runners.

Defined in ``jobs/`` rather than ``web/`` because the dependency direction is
one-way: ``web`` imports ``jobs``, never the reverse (spec section 4.1). The
web layer re-exports ``JobEvent`` and supplies the fan-out bus that implements
``EventPublisher``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from higgshole.store.db import ErrorReason, GenerationKind, GenerationState


@dataclass(frozen=True)
class JobEvent:
    """One state transition, broadcast to every listener."""

    generation_id: str
    kind: GenerationKind
    state: GenerationState
    error_reason: ErrorReason | None
    detail: str | None
    at: str

    def to_sse(self) -> str:
        """Render as a single Server-Sent Events frame."""
        payload = json.dumps(
            {
                "generation_id": self.generation_id,
                "kind": str(self.kind),
                "state": str(self.state),
                "error_reason": None if self.error_reason is None else str(self.error_reason),
                "detail": self.detail,
                "at": self.at,
            },
            sort_keys=True,
        )
        return f"event: job\ndata: {payload}\n\n"


@runtime_checkable
class EventPublisher(Protocol):
    """Anything the runners can hand a JobEvent to.

    Publishing must never block or raise: a slow browser tab must not stall a
    job runner, and a failed broadcast must not fail a paid generation.
    """

    def publish(self, event: JobEvent) -> None: ...


class NullEventPublisher:
    """Discards every event. The default when no bus has been wired."""

    def publish(self, event: JobEvent) -> None:
        return None
```

Create `src/higgshole/jobs/clock.py`:

```python
"""Injectable time.

Every wait in the job engine goes through a Clock so that the wall-clock
ceiling (spec section 4.3) and the retry backoff (spec section 4.4) can be
tested exhaustively without a test ever sleeping.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never affected by clock changes."""

    async def sleep(self, seconds: float) -> None:
        """Yield to the event loop for a duration."""


class RealClock:
    """The production clock. Monotonic, so an NTP step cannot un-expire a job."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_events.py -v`

Expected: PASS — `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/events.py src/higgshole/jobs/clock.py tests/jobs/test_events.py
git commit -m "feat: add job events and an injectable clock"
```

---

## Task 3: Provider status mapping and retry policy

**Files:**
- Create: `src/higgshole/jobs/runner.py` (the value types and mapping only; the runners arrive in Tasks 4–7)
- Test: `tests/jobs/test_status_mapping.py`

**Interfaces:**
- Consumes: `GenerationKind`, `GenerationState`, `ErrorReason`, `InputRole` from `higgshole.store.db`.
- Produces:
  - `GenerationRequest(kind, project_id, project_slug, model, prompt, params, inputs=())`
  - `GenerationOutcome(generation_id, state, file_path, asset_id, cost, error_reason, error_detail)`
  - `RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)` with `delay_for(attempt: int) -> float`
  - `map_provider_status(status: str) -> tuple[GenerationState, ErrorReason | None]`

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_status_mapping.py`:

```python
import pytest

from higgshole.jobs.runner import (
    GenerationRequest,
    RetryPolicy,
    map_provider_status,
)
from higgshole.store.db import ErrorReason, GenerationKind, GenerationState


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    [
        ("pending", GenerationState.RUNNING, None),
        ("in_progress", GenerationState.RUNNING, None),
        ("completed", GenerationState.DOWNLOADING, None),
        ("failed", GenerationState.FAILED, ErrorReason.PROVIDER_FAILED),
        ("cancelled", GenerationState.FAILED, ErrorReason.PROVIDER_CANCELLED),
        ("expired", GenerationState.FAILED, ErrorReason.PROVIDER_EXPIRED),
    ],
)
def test_every_documented_status_maps_as_the_specification_requires(status, state, reason):
    assert map_provider_status(status) == (state, reason)


def test_unrecognised_status_keeps_polling():
    # Spec section 2.4: over-polling is bounded by the wall-clock ceiling and
    # self-corrects; treating a live job as terminal loses a paid generation.
    assert map_provider_status("reticulating") == (GenerationState.RUNNING, None)
    assert map_provider_status("") == (GenerationState.RUNNING, None)


def test_every_terminal_provider_status_has_a_reason():
    for status in ("failed", "cancelled", "expired"):
        state, reason = map_provider_status(status)
        assert state is GenerationState.FAILED
        assert reason is not None


def test_retry_delay_grows_and_is_capped():
    policy = RetryPolicy(max_retries=8, base_delay_s=1.0, max_delay_s=30.0)

    # Full jitter means each delay is a sample from [0, ceiling], so the
    # ceiling is what is asserted, not the sample.
    assert all(policy.delay_for(attempt) <= 30.0 for attempt in range(10))
    assert max(policy.delay_for(0) for _ in range(200)) <= 1.0
    assert max(policy.delay_for(3) for _ in range(200)) <= 8.0


def test_retry_delay_is_never_negative():
    policy = RetryPolicy()

    assert all(policy.delay_for(attempt) >= 0.0 for attempt in range(-2, 6))


def test_generation_request_defaults_to_no_inputs():
    request = GenerationRequest(
        kind=GenerationKind.IMAGE,
        project_id="p1",
        project_slug="unsorted",
        model="a/b",
        prompt="a cat",
        params={},
    )

    assert request.inputs == ()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_status_mapping.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.jobs.runner'`

- [ ] **Step 3: Implement**

Create `src/higgshole/jobs/runner.py`:

```python
"""The two generation state machines (spec section 4.3).

Image and video have different shapes and deliberately do not share a machine:

    image:  PENDING -> GENERATING -> WRITING -> COMPLETE
    video:  PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE

with REJECTED and FAILED branches on both. Only video rows can ever occupy
SUBMITTED, RUNNING or DOWNLOADING, which is what makes boot-time reattachment
(jobs/resume.py) safe to scope by state.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from higgshole.store.db import (
    ErrorReason,
    GenerationKind,
    GenerationState,
    InputRole,
)


@dataclass(frozen=True)
class GenerationRequest:
    """A validated, project-resolved request.

    Built by web/api.py. The runner never parses HTTP input, so the same
    engine serves the REST API and any future caller unchanged.
    """

    kind: GenerationKind
    project_id: str
    project_slug: str
    model: str
    prompt: str
    params: dict[str, Any]
    #: (asset_id, role) pairs in the order the operator supplied them.
    inputs: tuple[tuple[str, InputRole], ...] = ()


@dataclass(frozen=True)
class GenerationOutcome:
    """What a runner returns to its caller."""

    generation_id: str
    state: GenerationState
    file_path: str | None
    asset_id: str | None
    cost: Decimal | None
    error_reason: ErrorReason | None
    error_detail: str | None


@dataclass(frozen=True)
class RetryPolicy:
    """Spec section 4.4.

    Submission is never blindly retried: POST /images is synchronous and
    non-idempotent, so a retry risks a second charge. Only 429-before-dispatch
    and idempotent GETs (poll, download) use this.
    """

    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at max_delay_s.

        Full jitter rather than a fixed schedule because several pollers may
        back off from the same 429 at the same instant; sampling from
        [0, ceiling] de-synchronises them.
        """
        exponent = max(0, attempt)
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**exponent))
        return random.uniform(0.0, ceiling)


#: Provider job status -> (internal state, error reason). Spec section 4.3.
_STATUS_MAP: dict[str, tuple[GenerationState, ErrorReason | None]] = {
    "pending": (GenerationState.RUNNING, None),
    "in_progress": (GenerationState.RUNNING, None),
    "completed": (GenerationState.DOWNLOADING, None),
    "failed": (GenerationState.FAILED, ErrorReason.PROVIDER_FAILED),
    "cancelled": (GenerationState.FAILED, ErrorReason.PROVIDER_CANCELLED),
    "expired": (GenerationState.FAILED, ErrorReason.PROVIDER_EXPIRED),
}


def map_provider_status(status: str) -> tuple[GenerationState, ErrorReason | None]:
    """Provider job status -> (internal state, error reason).

    Unrecognised statuses map to (RUNNING, None): over-polling is bounded by
    the wall-clock ceiling and self-corrects, while treating a live job as
    terminal loses a paid generation irrecoverably (spec section 2.4).
    """
    return _STATUS_MAP.get(status, (GenerationState.RUNNING, None))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_status_mapping.py -v`

Expected: PASS — `11 passed` (the first test is parametrized over six statuses)

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/runner.py tests/jobs/test_status_mapping.py
git commit -m "feat: add provider status mapping and jittered retry policy"
```

---

## Task 4: Shared runner plumbing

**Files:**
- Modify: `src/higgshole/jobs/runner.py` (add `JobRunner`)
- Create: `tests/jobs/fakes.py`
- Create: `tests/jobs/conftest.py`
- Test: `tests/jobs/test_runner_base.py`

**Interfaces:**
- Consumes: `Database`, `MediaPaths`, `BudgetGate`, `Reservation`, `GateRejection`, `GateDecision`, `CatalogCache`, `Settings`, `OpenRouterClient`, `EventPublisher`, `Clock`, `Estimate`, `estimate_image_cost`, `estimate_video_cost`, `validate_image_request`, `validate_video_request`, `has_hard_failure`, `atomic_write_bytes`, `discard_part`, `write_sidecar`, `SIDECAR_VERSION`, `file_size`, `probe_media`, `embed_params`, `extension_for`, `make_image_thumbnail`, `make_video_thumbnail`, `make_video_poster`.
- Produces:
  - `JobRunner(*, db, paths, gate, catalog, settings, client_factory, events, clock=None, retry_policy=None)`
  - `async create_pending(request: GenerationRequest) -> GenerationRow`
  - `async validate(request: GenerationRequest) -> list[ValidationIssue]`
  - `async estimate(request: GenerationRequest) -> Estimate`
  - `async reject(gen_id: str, reason: ErrorReason, detail: str) -> GenerationOutcome`
  - `async finalise_success(*, gen_id, data, media_type, cost, reservation) -> GenerationOutcome`
  - `async finalise_failure(*, gen_id, reason, detail, reservation) -> GenerationOutcome`

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/fakes.py`:

```python
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
```

Create `tests/jobs/conftest.py` — **fixtures only**; it must not define
`_forbid_real_network`, which is inherited from `tests/conftest.py`:

```python
"""Fixtures for the job engine tests.

This file adds fixtures and nothing else. The autouse network guard lives in
tests/conftest.py and must not be shadowed here.
"""

from __future__ import annotations

import pytest

from tests.jobs.fakes import Harness, fake_metadata_for, fake_thumbnail


@pytest.fixture
def stub_media(monkeypatch):
    """Replace probing, embedding and thumbnailing at the runner's seam.

    The real implementations shell out to ffprobe/ffmpeg and decode images
    with Pillow. Neither is what these tests are about, and requiring ffmpeg
    on every developer machine to test a state machine is a poor trade.
    """
    monkeypatch.setattr("higgshole.jobs.runner.probe_media", fake_metadata_for)
    monkeypatch.setattr("higgshole.jobs.runner.embed_params", lambda path, payload: None)
    monkeypatch.setattr("higgshole.jobs.runner.make_image_thumbnail", fake_thumbnail)
    monkeypatch.setattr("higgshole.jobs.runner.make_video_thumbnail", fake_thumbnail)
    monkeypatch.setattr("higgshole.jobs.runner.make_video_poster", fake_thumbnail)


@pytest.fixture
def harness(tmp_path, stub_media):
    built = Harness(tmp_path)
    yield built
    built.db.close()
```

Create `tests/jobs/test_runner_base.py`:

```python
from decimal import Decimal

import pytest

from higgshole.budget.estimator import Estimate
from higgshole.catalog.validation import Severity, has_hard_failure
from higgshole.store.db import (
    AssetKind,
    ErrorReason,
    GenerationState,
    InputRole,
    LedgerKind,
)
from tests.jobs.fakes import PNG_BYTES


async def _reserve(harness, gen_id):
    """Take a real reservation through the gate, as the runners do."""
    return await harness.gate.acquire(
        generation_id=gen_id,
        estimate=Estimate(amount=Decimal("0.10"), reason=None, detail="exact"),
    )


async def test_create_pending_inserts_a_pending_row(harness):
    row = await harness.image_runner.create_pending(harness.image_request())

    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.PENDING
    assert stored.model == "test/image-model"
    assert harness.events.states_for(row.id) == ["PENDING"]


async def test_create_pending_records_inputs_in_order(harness):
    first = harness.upload("first.png")
    second = harness.upload("second.png")

    row = await harness.image_runner.create_pending(
        harness.image_request(
            inputs=(
                (first, InputRole.INPUT_REFERENCE),
                (second, InputRole.INPUT_REFERENCE),
            )
        )
    )

    links = harness.db.list_generation_inputs(row.id)
    assert [link.asset_id for link in links] == [first, second]
    assert [link.position for link in links] == [0, 1]


async def test_validate_flags_an_unknown_model_as_hard(harness):
    issues = await harness.image_runner.validate(
        harness.image_request(model="nobody/nothing")
    )

    assert has_hard_failure(issues) is True
    assert issues[0].parameter == "model"


async def test_validate_passes_a_supported_image_request(harness):
    assert await harness.image_runner.validate(harness.image_request()) == []


async def test_validate_rejects_batch_generation(harness):
    # Spec section 5.5: n is fixed at 1.
    issues = await harness.image_runner.validate(
        harness.image_request(params={"n": 4, "quality": "high"})
    )

    assert has_hard_failure(issues) is True
    assert any(issue.parameter == "n" for issue in issues)


async def test_reject_moves_the_row_to_rejected_and_emits_an_event(harness):
    row = await harness.image_runner.create_pending(harness.image_request())

    outcome = await harness.image_runner.reject(
        row.id, ErrorReason.VALIDATION, "unsupported resolution"
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION
    assert harness.db.get_generation(row.id).state is GenerationState.REJECTED
    assert harness.events.states_for(row.id) == ["PENDING", "REJECTED"]


async def test_finalise_success_writes_media_sidecar_and_asset(harness):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    media_path = harness.paths.root / outcome.file_path
    assert media_path.read_bytes() == PNG_BYTES
    assert harness.paths.sidecar_path(media_path).exists()
    assert not media_path.with_suffix(media_path.suffix + ".part").exists()

    assets = harness.db.list_assets_for_generation(row.id)
    kinds = {asset.kind for asset in assets}
    assert AssetKind.OUTPUT in kinds
    assert AssetKind.THUMBNAIL in kinds


async def test_finalise_success_marks_the_row_complete_and_releases_the_reservation(
    harness,
):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.COMPLETE
    assert stored.completed_at is not None
    assert stored.file_path == harness.db.get_generation(row.id).file_path

    kinds = [row_.kind for row_ in harness.db.list_ledger_for_generation(row.id)]
    assert LedgerKind.ACTUAL in kinds
    assert harness.ledger_total(row.id) == Decimal("0.04")


async def test_finalise_success_survives_a_metadata_embedding_failure(
    harness, monkeypatch
):
    # The sidecar is the authoritative record; embedding is a convenience, and
    # a metadata failure must never fail a paid generation.
    def _boom(path, payload):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr("higgshole.jobs.runner.embed_params", _boom)

    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    assert outcome.state is GenerationState.COMPLETE


async def test_finalise_failure_marks_failed_and_reverses_the_reservation(harness):
    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_failure(
        gen_id=row.id,
        reason=ErrorReason.DOWNLOAD_FAILED,
        detail="upstream 502",
        reservation=reservation,
    )

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.DOWNLOAD_FAILED
    # A failed job nets to zero (spec section 3.3).
    assert harness.ledger_total(row.id) == Decimal("0")


async def test_finalise_failure_discards_a_stale_part_file(harness, monkeypatch):
    # An interrupted write leaves only the .part file, which is never renamed,
    # so a half-file can never be indexed as a complete asset (spec section 10).
    def _explode(path, data, **kwargs):
        part = path.with_name(path.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr("higgshole.jobs.runner.atomic_write_bytes", _explode)

    row = await harness.image_runner.create_pending(harness.image_request())
    reservation = await _reserve(harness, row.id)

    outcome = await harness.image_runner.finalise_success(
        gen_id=row.id,
        data=PNG_BYTES,
        media_type="image/png",
        cost=Decimal("0.04"),
        reservation=reservation,
    )

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.WRITE_FAILED
    parts = list(harness.paths.root.rglob("*.part"))
    assert parts == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_runner_base.py -v`

Expected: FAIL — `ImportError: cannot import name 'ImageJobRunner' from 'higgshole.jobs.runner'`

- [ ] **Step 3: Implement**

Extend the imports at the top of `src/higgshole/jobs/runner.py`:

```python
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from higgshole.budget.estimator import (
    Estimate,
    EstimateUnavailable,
    estimate_image_cost,
    estimate_video_cost,
)
from higgshole.budget.gate import BudgetGate, Reservation
from higgshole.catalog.validation import (
    Severity,
    ValidationIssue,
    validate_image_request,
    validate_video_request,
)
from higgshole.config import MediaKind, Settings
from higgshole.jobs.clock import Clock, RealClock
from higgshole.jobs.events import EventPublisher, JobEvent
from higgshole.jobs.references import (
    build_input_references,
    build_video_frame_images,
)
from higgshole.orclient.client import OpenRouterClient
from higgshole.store.db import (
    AssetKind,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    utc_now_iso,
)
from higgshole.store.files import (
    SIDECAR_VERSION,
    atomic_write_bytes,
    discard_part,
    file_size,
    write_sidecar,
)
from higgshole.store.metadata import (
    embed_params,
    extension_for,
    make_image_thumbnail,
    make_video_poster,
    make_video_thumbnail,
    probe_media,
)
from higgshole.store.paths import MediaPaths

logger = logging.getLogger(__name__)

#: Parameters that are never forwarded to the provider. ``n`` is fixed at 1
#: (spec section 5.5) and is rejected by validation rather than transmitted.
_NON_WIRE_PARAMS: frozenset[str] = frozenset({"n"})
```

Append the `JobRunner` class to `src/higgshole/jobs/runner.py`:

```python
class JobRunner:
    """Shared plumbing for both machines.

    Database calls are made directly on the event loop thread rather than
    through a worker: each is a sub-millisecond local SQLite statement, and a
    sqlite3 connection is bound to its creating thread, so hopping threads
    would require reopening it per call for no measurable gain.
    """

    #: Which API key the subclass draws from (spec section 8).
    media_kind: MediaKind = "image"

    def __init__(
        self,
        *,
        db: Database,
        paths: MediaPaths,
        gate: BudgetGate,
        catalog: Any,
        settings: Settings,
        client_factory: Callable[[MediaKind], OpenRouterClient],
        events: EventPublisher,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self.gate = gate
        self.catalog = catalog
        self.settings = settings
        self.client_factory = client_factory
        self.events = events
        self.clock = clock or RealClock()
        self.retry_policy = retry_policy or RetryPolicy(max_retries=settings.max_retries)
        #: Output paths whose .part file must be discarded if the job fails.
        self._parts: dict[str, Path] = {}
        #: Provider-side generation IDs, recorded for the sidecar.
        self._provider_generation_ids: dict[str, str] = {}

    # -- events ----------------------------------------------------------

    def _publish(
        self,
        gen_id: str,
        kind: GenerationKind,
        state: GenerationState,
        reason: ErrorReason | None = None,
        detail: str | None = None,
    ) -> None:
        self.events.publish(
            JobEvent(
                generation_id=gen_id,
                kind=kind,
                state=state,
                error_reason=reason,
                detail=detail,
                at=utc_now_iso(),
            )
        )

    def _transition(
        self,
        gen_id: str,
        kind: GenerationKind,
        state: GenerationState,
        *,
        reason: ErrorReason | None = None,
        detail: str | None = None,
        completed_at: str | None = None,
    ) -> GenerationRow:
        row = self.db.set_generation_state(
            gen_id,
            state,
            error_reason=reason,
            error_detail=detail,
            completed_at=completed_at,
        )
        self._publish(gen_id, kind, state, reason, detail)
        return row

    # -- creation and validation ------------------------------------------

    async def create_pending(self, request: GenerationRequest) -> GenerationRow:
        """Insert the generation in PENDING and record its inputs."""
        row = self.db.create_generation(
            project_id=request.project_id,
            kind=request.kind,
            model=request.model,
            prompt=request.prompt,
            params=dict(request.params),
            state=GenerationState.PENDING,
        )
        for position, (asset_id, role) in enumerate(request.inputs):
            self.db.add_generation_input(
                generation_id=row.id,
                asset_id=asset_id,
                role=role,
                position=position,
            )
        self._publish(row.id, request.kind, GenerationState.PENDING)
        return row

    @staticmethod
    def _unknown_model_issue(model_id: str) -> ValidationIssue:
        return ValidationIssue(
            parameter="model",
            value=model_id,
            severity=Severity.HARD,
            message=(
                f"{model_id} is not in the cached catalogue. Refresh the "
                "catalogue from Settings, or check the model identifier."
            ),
        )

    @staticmethod
    def _frame_types(request: GenerationRequest) -> list[str]:
        return [
            str(role)
            for _, role in request.inputs
            if role in (InputRole.FIRST_FRAME, InputRole.LAST_FRAME)
        ]

    @staticmethod
    def _reference_count(request: GenerationRequest) -> int:
        return sum(1 for _, role in request.inputs if role is InputRole.INPUT_REFERENCE)

    async def validate(self, request: GenerationRequest) -> list[ValidationIssue]:
        """Run catalog.validation against cached capabilities.

        Ordering is fixed: local validation -> budget gate -> dispatch
        (spec section 4.3), so an invalid combination costs nothing rather
        than becoming a failed paid request.
        """
        if request.kind is GenerationKind.IMAGE:
            model = await self.catalog.get_image_model(request.model)
            if model is None:
                return [self._unknown_model_issue(request.model)]
            return validate_image_request(
                model,
                n=int(request.params.get("n", 1)),
                quality=request.params.get("quality"),
                reference_count=self._reference_count(request),
                daily_cap_set=self.gate.cap_is_set,
            )

        model = await self.catalog.get_video_model(request.model)
        if model is None:
            return [self._unknown_model_issue(request.model)]
        return validate_video_request(
            model,
            resolution=request.params.get("resolution"),
            aspect_ratio=request.params.get("aspect_ratio"),
            duration=request.params.get("duration"),
            frame_types=self._frame_types(request),
        )

    async def estimate(self, request: GenerationRequest) -> Estimate:
        """The advisory pre-flight cost, or an explicit reason it is unknown.

        Never returns a fabricated number: where the axes do not resolve, the
        gate falls back to the pessimistic ceiling instead (spec section 3.3).
        """
        if request.kind is GenerationKind.IMAGE:
            model = await self.catalog.get_image_model(request.model)
            if model is None:
                return Estimate(
                    amount=None,
                    reason=EstimateUnavailable.NO_PRICING_DATA,
                    detail=f"{request.model} is not in the cached catalogue.",
                )
            pricing = await self.catalog.get_image_pricing(request.model)
            return estimate_image_cost(
                model,
                pricing,
                width=request.params.get("width"),
                height=request.params.get("height"),
                quality=request.params.get("quality"),
                reference_count=self._reference_count(request),
            )

        model = await self.catalog.get_video_model(request.model)
        if model is None:
            return Estimate(
                amount=None,
                reason=EstimateUnavailable.NO_PRICING_DATA,
                detail=f"{request.model} is not in the cached catalogue.",
            )
        return estimate_video_cost(
            model,
            duration=request.params.get("duration"),
            resolution=request.params.get("resolution"),
            aspect_ratio=request.params.get("aspect_ratio"),
            generate_audio=bool(request.params.get("generate_audio")),
            has_frame_images=bool(self._frame_types(request)),
        )

    async def reject(
        self, gen_id: str, reason: ErrorReason, detail: str
    ) -> GenerationOutcome:
        """Move to REJECTED and emit an event.

        No reservation is settled because rejection happens before or at the
        gate: either nothing was reserved, or the gate refused to reserve.
        """
        row = self.db.get_generation(gen_id)
        kind = row.kind if row is not None else GenerationKind.IMAGE
        self._transition(gen_id, kind, GenerationState.REJECTED, reason=reason, detail=detail)
        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.REJECTED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=reason,
            error_detail=detail,
        )

    # -- resolving stored inputs into provider payloads --------------------

    def _resolved_inputs(self, gen_id: str) -> list[tuple[Any, InputRole]]:
        resolved: list[tuple[Any, InputRole]] = []
        for link in self.db.list_generation_inputs(gen_id):
            asset = self.db.get_asset(link.asset_id)
            if asset is not None:
                resolved.append((asset, link.role))
        return resolved

    def input_references_for(self, gen_id: str) -> list[str]:
        return build_input_references(
            self._resolved_inputs(gen_id),
            self.paths,
            transport=self.settings.reference_transport,
        )

    def frame_images_for(self, gen_id: str) -> list[tuple[str, str]]:
        return build_video_frame_images(
            self._resolved_inputs(gen_id),
            self.paths,
            transport=self.settings.reference_transport,
        )

    @staticmethod
    def wire_params(params: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in params.items()
            if key not in _NON_WIRE_PARAMS and value is not None
        }

    # -- completion --------------------------------------------------------

    def _sidecar_payload(
        self,
        *,
        row: GenerationRow,
        project_slug: str,
        relative: str,
        metadata: Any,
        cost: Decimal | None,
        completed_at: str,
    ) -> dict[str, Any]:
        inputs: list[dict[str, Any]] = []
        for link in self.db.list_generation_inputs(row.id):
            asset = self.db.get_asset(link.asset_id)
            inputs.append(
                {
                    "asset_id": link.asset_id,
                    "role": str(link.role),
                    "position": link.position,
                    "relative_path": None if asset is None else asset.file_path,
                }
            )
        return {
            "sidecar_version": SIDECAR_VERSION,
            "id": row.id,
            "kind": str(row.kind),
            "project_slug": project_slug,
            "model": row.model,
            "prompt": row.prompt,
            "params": dict(row.params),
            "inputs": inputs,
            "provider": {
                "job_id": row.provider_job_id,
                "generation_id": self._provider_generation_ids.get(row.id),
            },
            "media": {
                "relative_path": relative,
                "mime_type": metadata.mime_type,
                "bytes": metadata.bytes,
                "width": metadata.width,
                "height": metadata.height,
                "duration_s": metadata.duration_s,
            },
            "cost": {
                "amount_usd": None if cost is None else str(cost),
                "known": cost is not None,
            },
            "created_at": row.created_at,
            "completed_at": completed_at,
        }

    def _write_derived_images(
        self, *, row: GenerationRow, project_slug: str, media_path: Path
    ) -> None:
        """Thumbnail and, for video, a poster frame.

        Wrapped: a thumbnail failure degrades the library grid, and failing a
        paid generation over it would be indefensible.
        """
        try:
            thumb = self.paths.thumb_path(project_slug=project_slug, gen_id=row.id)
            if row.kind is GenerationKind.VIDEO:
                poster = self.paths.poster_path(project_slug=project_slug, gen_id=row.id)
                poster_meta = make_video_poster(media_path, poster)
                self.db.create_asset(
                    kind=AssetKind.POSTER,
                    file_path=poster.relative_to(self.paths.root).as_posix(),
                    mime_type=poster_meta.mime_type,
                    bytes_=poster_meta.bytes,
                    generation_id=row.id,
                    width=poster_meta.width,
                    height=poster_meta.height,
                )
                thumb_meta = make_video_thumbnail(media_path, thumb)
            else:
                thumb_meta = make_image_thumbnail(media_path, thumb)

            self.db.create_asset(
                kind=AssetKind.THUMBNAIL,
                file_path=thumb.relative_to(self.paths.root).as_posix(),
                mime_type=thumb_meta.mime_type,
                bytes_=thumb_meta.bytes,
                generation_id=row.id,
                width=thumb_meta.width,
                height=thumb_meta.height,
            )
        except Exception:
            logger.warning("thumbnailing failed for %s", row.id, exc_info=True)

    async def finalise_success(
        self,
        *,
        gen_id: str,
        data: bytes,
        media_type: str,
        cost: Decimal | None,
        reservation: Reservation | None,
    ) -> GenerationOutcome:
        """The single completion path for both machines."""
        row = self.db.get_generation(gen_id)
        project = self.db.get_project(row.project_id)

        try:
            allocated = self.paths.allocate_output(
                project_slug=project.slug,
                kind=row.kind,
                gen_id=gen_id,
                prompt=row.prompt,
                ext=extension_for(media_type),
            )
        except Exception as exc:
            return await self.finalise_failure(
                gen_id=gen_id,
                reason=ErrorReason.WRITE_FAILED,
                detail=f"could not allocate an output path: {exc}",
                reservation=reservation,
            )

        self._parts[gen_id] = allocated.media_path
        try:
            atomic_write_bytes(allocated.media_path, data)
            metadata = probe_media(allocated.media_path)
        except Exception as exc:
            return await self.finalise_failure(
                gen_id=gen_id,
                reason=ErrorReason.WRITE_FAILED,
                detail=str(exc),
                reservation=reservation,
            )
        self._parts.pop(gen_id, None)

        relative = allocated.relative_media_path.as_posix()
        completed_at = utc_now_iso()
        payload = self._sidecar_payload(
            row=row,
            project_slug=project.slug,
            relative=relative,
            metadata=metadata,
            cost=cost,
            completed_at=completed_at,
        )

        # The sidecar is written before the tag is embedded so that an
        # embedding failure can never leave the authoritative record unwritten.
        # media.bytes therefore records the file as generated; the asset row
        # below re-stats the file so the served length is always the truth.
        write_sidecar(allocated.sidecar_path, payload)
        try:
            embed_params(allocated.media_path, payload)
        except Exception:
            logger.warning("embedding parameters failed for %s", gen_id, exc_info=True)

        asset = self.db.create_asset(
            kind=AssetKind.OUTPUT,
            file_path=relative,
            mime_type=metadata.mime_type,
            bytes_=file_size(allocated.media_path),
            generation_id=gen_id,
            width=metadata.width,
            height=metadata.height,
            duration_s=metadata.duration_s,
        )
        self._write_derived_images(
            row=row, project_slug=project.slug, media_path=allocated.media_path
        )

        self.db.set_generation_file(gen_id, relative)
        self._transition(
            gen_id, row.kind, GenerationState.COMPLETE, completed_at=completed_at
        )

        if reservation is not None:
            await self.gate.release(reservation, actual_cost=cost, succeeded=True)

        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.COMPLETE,
            file_path=relative,
            asset_id=asset.id,
            cost=cost,
            error_reason=None,
            error_detail=None,
        )

    async def finalise_failure(
        self,
        *,
        gen_id: str,
        reason: ErrorReason,
        detail: str,
        reservation: Reservation | None,
    ) -> GenerationOutcome:
        """FAILED plus gate.release(succeeded=False). Discards any .part file."""
        part_owner = self._parts.pop(gen_id, None)
        if part_owner is not None:
            discard_part(part_owner)

        row = self.db.get_generation(gen_id)
        kind = row.kind if row is not None else GenerationKind.IMAGE
        self._transition(
            gen_id, kind, GenerationState.FAILED, reason=reason, detail=detail
        )

        if reservation is not None:
            await self.gate.release(reservation, actual_cost=None, succeeded=False)

        return GenerationOutcome(
            generation_id=gen_id,
            state=GenerationState.FAILED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=reason,
            error_detail=detail,
        )
```

Because `tests/jobs/fakes.py` imports `ImageJobRunner` and `VideoJobRunner`,
add temporary subclasses at the end of the module so this task can go green on
its own; Tasks 5 and 6 replace their bodies:

```python
class ImageJobRunner(JobRunner):
    """PENDING -> GENERATING -> WRITING -> COMPLETE (spec section 4.3)."""

    media_kind: MediaKind = "image"


class VideoJobRunner(JobRunner):
    """PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE."""

    media_kind: MediaKind = "video"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_runner_base.py -v`

Expected: PASS — `11 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/runner.py tests/jobs/fakes.py tests/jobs/conftest.py tests/jobs/test_runner_base.py
git commit -m "feat: add shared job runner plumbing and completion path"
```

---

## Task 5: The image state machine

**Files:**
- Modify: `src/higgshole/jobs/runner.py` (implement `ImageJobRunner.run`)
- Test: `tests/jobs/test_image_runner.py`

**Interfaces:**
- Consumes: `JobRunner` (Task 4), `OpenRouterClient.generate_image`, `GateRejection`, `GateDecision`, the error hierarchy from `higgshole.orclient.errors`.
- Produces: `ImageJobRunner.run(request: GenerationRequest) -> GenerationOutcome`, plus the shared helper `JobRunner.gate_or_reject(gen_id, request) -> Reservation | GenerationOutcome`.

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_image_runner.py`:

```python
from decimal import Decimal

from higgshole.orclient.errors import (
    IndeterminateError,
    InsufficientCreditsError,
    ModerationError,
    RateLimitError,
)
from higgshole.store.db import ErrorReason, GenerationState, InputRole, LedgerKind
from tests.jobs.fakes import Harness, video_job


async def test_a_successful_image_run_reaches_complete(harness):
    harness.client.image_results.append(harness.image_result())

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert outcome.cost == Decimal("0.04")
    assert (harness.paths.root / outcome.file_path).exists()


async def test_the_state_sequence_is_pending_generating_writing_complete(harness):
    harness.client.image_results.append(harness.image_result())

    outcome = await harness.image_runner.run(harness.image_request())

    assert harness.events.states_for(outcome.generation_id) == [
        "PENDING",
        "GENERATING",
        "WRITING",
        "COMPLETE",
    ]


async def test_hard_validation_failure_is_rejected_before_dispatch(harness):
    outcome = await harness.image_runner.run(
        harness.image_request(params={"quality": "ultra"})
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION


async def test_a_rejected_request_never_calls_the_provider(harness):
    await harness.image_runner.run(harness.image_request(model="nobody/nothing"))

    assert harness.client.calls == []


async def test_cap_rejection_maps_to_cap_exceeded(tmp_path, stub_media):
    # Cap of 0.01 with a pessimistic ceiling of 2.00 cannot admit anything.
    harness = Harness(tmp_path, daily_cap_usd=Decimal("0.01"))
    try:
        outcome = await harness.image_runner.run(
            harness.image_request(params={"quality": "high"})
        )

        assert outcome.state is GenerationState.REJECTED
        assert outcome.error_reason is ErrorReason.CAP_EXCEEDED
        assert harness.client.calls == []
    finally:
        harness.db.close()


async def test_in_flight_rejection_maps_to_in_flight_limit(tmp_path, stub_media):
    harness = Harness(tmp_path, max_in_flight=1)
    try:
        # Occupy the only slot with a video row that never terminates.
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(50)
        )
        await harness.video_runner.submit(harness.video_request())

        outcome = await harness.image_runner.run(harness.image_request())

        assert outcome.state is GenerationState.REJECTED
        assert outcome.error_reason is ErrorReason.IN_FLIGHT_LIMIT
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_indeterminate_error_fails_without_retry(harness):
    # POST /images is synchronous and non-idempotent, so a retry risks a
    # second charge (spec section 4.4).
    harness.client.image_results.append(IndeterminateError("connection reset"))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.INDETERMINATE
    assert harness.client.call_names().count("generate_image") == 1


async def test_a_rate_limit_is_retried_before_dispatch(harness):
    harness.client.image_results.extend(
        [RateLimitError("slow down", status_code=429), harness.image_result()]
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert harness.client.call_names().count("generate_image") == 2
    assert harness.clock.slept  # backoff went through the injected clock


async def test_a_moderation_refusal_maps_to_moderation(harness):
    harness.client.image_results.append(
        ModerationError("Content policy violation", status_code=400)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.error_reason is ErrorReason.MODERATION
    assert "policy" in outcome.error_detail.lower()


async def test_insufficient_credits_maps_to_its_own_reason(harness):
    # Surfaced distinctly from the local cap so the operator knows which
    # guard tripped (spec section 10).
    harness.client.image_results.append(
        InsufficientCreditsError("credit limit reached", status_code=402)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.error_reason is ErrorReason.INSUFFICIENT_CREDITS


async def test_a_null_cost_leaves_the_reservation_standing(harness):
    # Spec section 3.4: the reservation stands as the recorded charge and the
    # day is marked a lower bound. Zero is never recorded as the charge.
    harness.client.image_results.append(harness.image_result(cost=None))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    assert outcome.cost is None

    rows = harness.db.list_ledger_for_generation(outcome.generation_id)
    kinds = [row.kind for row in rows]
    assert LedgerKind.RESERVATION in kinds
    assert LedgerKind.REVERSAL not in kinds
    assert harness.ledger_total(outcome.generation_id) > Decimal("0")
    assert any(row.cost_known is False for row in rows)


async def test_input_references_are_sent_as_data_uris(harness):
    asset_id = harness.upload("ref.png")
    harness.client.image_results.append(harness.image_result())

    await harness.image_runner.run(
        harness.image_request(inputs=((asset_id, InputRole.INPUT_REFERENCE),))
    )

    sent = harness.client.last_call("generate_image")["input_references"]
    assert len(sent) == 1
    assert sent[0].startswith("data:image/png;base64,")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_image_runner.py -v`

Expected: FAIL — `AttributeError: 'ImageJobRunner' object has no attribute 'run'`

- [ ] **Step 3: Implement**

Add these imports to `src/higgshole/jobs/runner.py`:

```python
from higgshole.budget.gate import BudgetGate, GateDecision, GateRejection, Reservation
from higgshole.catalog.validation import has_hard_failure
from higgshole.orclient.errors import (
    IndeterminateError,
    InsufficientCreditsError,
    ModerationError,
    OpenRouterError,
    RateLimitError,
)
```

Add these members to `JobRunner` (they are shared by both machines):

```python
    #: Provider error type -> the reason recorded against the generation.
    _ERROR_REASONS: tuple[tuple[type[OpenRouterError], ErrorReason], ...] = (
        (IndeterminateError, ErrorReason.INDETERMINATE),
        (ModerationError, ErrorReason.MODERATION),
        (InsufficientCreditsError, ErrorReason.INSUFFICIENT_CREDITS),
    )

    @classmethod
    def reason_for(cls, exc: OpenRouterError) -> ErrorReason:
        for error_type, reason in cls._ERROR_REASONS:
            if isinstance(exc, error_type):
                return reason
        return ErrorReason.PROVIDER_FAILED

    @staticmethod
    def _rejection_reason(rejection: GateRejection) -> ErrorReason:
        if rejection.decision is GateDecision.IN_FLIGHT_LIMIT:
            return ErrorReason.IN_FLIGHT_LIMIT
        return ErrorReason.CAP_EXCEEDED

    async def gate_or_reject(
        self, gen_id: str, request: GenerationRequest
    ) -> Reservation | GenerationOutcome:
        """Estimate, then take the serialized budget gate.

        A GateRejection is a normal result, not an error: it becomes REJECTED
        with the matching reason and the provider is never called.
        """
        estimate = await self.estimate(request)
        decision = await self.gate.acquire(generation_id=gen_id, estimate=estimate)
        if isinstance(decision, GateRejection):
            return await self.reject(
                gen_id, self._rejection_reason(decision), decision.message
            )
        return decision

    async def validate_or_reject(
        self, gen_id: str, request: GenerationRequest
    ) -> GenerationOutcome | None:
        issues = await self.validate(request)
        if not has_hard_failure(issues):
            return None
        detail = "; ".join(
            issue.message for issue in issues if issue.severity is Severity.HARD
        )
        return await self.reject(gen_id, ErrorReason.VALIDATION, detail)
```

Replace the placeholder `ImageJobRunner` with:

```python
class ImageJobRunner(JobRunner):
    """PENDING -> GENERATING -> WRITING -> COMPLETE, with REJECTED and FAILED
    branches (spec section 4.3). Synchronous end to end; no row it creates can
    ever occupy SUBMITTED, RUNNING or DOWNLOADING.
    """

    media_kind: MediaKind = "image"

    async def run(self, request: GenerationRequest) -> GenerationOutcome:
        """Validate, gate, dispatch and persist in one call.

        A transport failure after the request is sent surfaces as
        IndeterminateError and becomes FAILED/INDETERMINATE — never retried,
        because POST /images is synchronous and non-idempotent (spec section
        4.4). HTTP 429 is retried with backoff before dispatch is considered
        to have occurred.
        """
        row = await self.create_pending(request)

        rejected = await self.validate_or_reject(row.id, request)
        if rejected is not None:
            return rejected

        gated = await self.gate_or_reject(row.id, request)
        if isinstance(gated, GenerationOutcome):
            return gated
        reservation = gated

        self._transition(row.id, GenerationKind.IMAGE, GenerationState.GENERATING)

        references = self.input_references_for(row.id)
        params = self.wire_params(request.params)

        attempt = 0
        while True:
            try:
                async with self.client_factory(self.media_kind) as client:
                    result = await client.generate_image(
                        model=request.model,
                        prompt=request.prompt,
                        input_references=references,
                        **params,
                    )
                break
            except RateLimitError as exc:
                # Retryable: a 429 means the request was refused before the
                # provider began work, so no charge can have occurred.
                if attempt >= self.retry_policy.max_retries:
                    return await self.finalise_failure(
                        gen_id=row.id,
                        reason=ErrorReason.PROVIDER_FAILED,
                        detail=f"rate limited after {attempt + 1} attempts: {exc}",
                        reservation=reservation,
                    )
                await self.clock.sleep(self.retry_policy.delay_for(attempt))
                attempt += 1
            except OpenRouterError as exc:
                return await self.finalise_failure(
                    gen_id=row.id,
                    reason=self.reason_for(exc),
                    detail=exc.message,
                    reservation=reservation,
                )

        self._transition(row.id, GenerationKind.IMAGE, GenerationState.WRITING)

        return await self.finalise_success(
            gen_id=row.id,
            data=result.data,
            media_type=result.media_type,
            cost=result.cost,
            reservation=reservation,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_image_runner.py -v`

Expected: PASS — `12 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/runner.py tests/jobs/test_image_runner.py
git commit -m "feat: implement the synchronous image generation state machine"
```

---

## Task 6: Video submission and poller attachment

**Files:**
- Modify: `src/higgshole/jobs/runner.py` (implement `VideoJobRunner.submit`, `attach_poller`, `active_pollers`, `shutdown`)
- Test: `tests/jobs/test_video_submit.py`

**Interfaces:**
- Consumes: `OpenRouterClient.submit_video`, `Database.set_provider_job_id`.
- Produces:
  - `VideoJobRunner.submit(request) -> GenerationOutcome`
  - `VideoJobRunner.attach_poller(gen_id, *, reservation) -> asyncio.Task[GenerationOutcome]`
  - `VideoJobRunner.active_pollers() -> Mapping[str, asyncio.Task[GenerationOutcome]]`
  - `async VideoJobRunner.shutdown(*, timeout_s: float = 10.0) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_video_submit.py`:

```python
import asyncio

import pytest

from higgshole.orclient.errors import IndeterminateError, RateLimitError
from higgshole.store.db import ErrorReason, GenerationState, InputRole
from tests.jobs.fakes import video_job


@pytest.fixture
def submitted(harness):
    """A video runner scripted to accept one submission and poll forever."""
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        video_job("job-1", "in_progress") for _ in range(50)
    )
    return harness


async def test_submit_returns_as_soon_as_the_job_id_is_committed(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())

    assert outcome.state is GenerationState.SUBMITTED
    assert outcome.file_path is None
    await submitted.video_runner.shutdown()


async def test_the_provider_job_id_is_persisted_before_polling_starts(submitted):
    # Spec section 4.3 durability rule: if the process dies here, resume.py
    # can still find the job.
    outcome = await submitted.video_runner.submit(submitted.video_request())

    stored = submitted.db.get_generation(outcome.generation_id)
    assert stored.provider_job_id == "job-1"
    assert stored.state is GenerationState.SUBMITTED
    await submitted.video_runner.shutdown()


async def test_submit_attaches_exactly_one_poller_per_generation(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())

    pollers = submitted.video_runner.active_pollers()
    assert list(pollers) == [outcome.generation_id]
    await submitted.video_runner.shutdown()


async def test_attach_poller_is_idempotent(submitted):
    # Boot reattachment must never double-download a paid generation.
    outcome = await submitted.video_runner.submit(submitted.video_request())

    first = submitted.video_runner.active_pollers()[outcome.generation_id]
    second = submitted.video_runner.attach_poller(
        outcome.generation_id, reservation=None
    )

    assert second is first
    await submitted.video_runner.shutdown()


async def test_validation_failure_is_rejected_before_submit(harness):
    outcome = await harness.video_runner.submit(
        harness.video_request(params={"duration": 7, "resolution": "720p"})
    )

    assert outcome.state is GenerationState.REJECTED
    assert outcome.error_reason is ErrorReason.VALIDATION
    assert harness.client.calls == []


async def test_indeterminate_submit_failure_is_never_retried(harness):
    harness.client.submit_results.append(IndeterminateError("reset after send"))

    outcome = await harness.video_runner.submit(harness.video_request())

    assert outcome.state is GenerationState.FAILED
    assert outcome.error_reason is ErrorReason.INDETERMINATE
    assert harness.client.call_names().count("submit_video") == 1


async def test_a_rate_limit_on_submit_is_retried(harness):
    harness.client.submit_results.extend(
        [RateLimitError("slow down", status_code=429), video_job("job-9", "pending")]
    )
    harness.client.poll_results.extend(
        video_job("job-9", "in_progress") for _ in range(50)
    )

    outcome = await harness.video_runner.submit(harness.video_request())

    assert outcome.state is GenerationState.SUBMITTED
    assert harness.client.call_names().count("submit_video") == 2
    await harness.video_runner.shutdown()


async def test_frame_images_are_sent_with_their_frame_type(harness):
    asset_id = harness.upload("first.png")
    harness.client.submit_results.append(video_job("job-2", "pending"))
    harness.client.poll_results.extend(
        video_job("job-2", "in_progress") for _ in range(50)
    )

    await harness.video_runner.submit(
        harness.video_request(inputs=((asset_id, InputRole.FIRST_FRAME),))
    )

    sent = harness.client.last_call("submit_video")
    assert [frame_type for _, frame_type in sent["frame_images"]] == ["first_frame"]
    assert sent["input_references"] == []
    await harness.video_runner.shutdown()


async def test_shutdown_cancels_every_poller(submitted):
    outcome = await submitted.video_runner.submit(submitted.video_request())
    task = submitted.video_runner.active_pollers()[outcome.generation_id]

    await submitted.video_runner.shutdown()

    assert task.done()
    assert submitted.video_runner.active_pollers() == {}
    # Rows left mid-flight are picked up by resume.py at the next boot.
    assert submitted.db.get_generation(outcome.generation_id).state in {
        GenerationState.SUBMITTED,
        GenerationState.RUNNING,
    }
    assert isinstance(asyncio.get_running_loop(), asyncio.AbstractEventLoop)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_video_submit.py -v`

Expected: FAIL — `AttributeError: 'VideoJobRunner' object has no attribute 'submit'`

- [ ] **Step 3: Implement**

Add to the imports in `src/higgshole/jobs/runner.py`:

```python
import asyncio
from collections.abc import Callable, Mapping

from higgshole.orclient.types import VideoJob
```

Replace the placeholder `VideoJobRunner` with:

```python
class VideoJobRunner(JobRunner):
    """PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE, with
    REJECTED and FAILED branches (spec section 4.3).
    """

    media_kind: MediaKind = "video"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pollers: dict[str, asyncio.Task[GenerationOutcome]] = {}

    async def submit(self, request: GenerationRequest) -> GenerationOutcome:
        """Validate, gate, submit, persist the provider job ID, start polling.

        Returns as soon as the ID is committed and never blocks on the render:
        a multi-minute wait inside one call invites client timeouts
        (spec section 6.2).
        """
        row = await self.create_pending(request)

        rejected = await self.validate_or_reject(row.id, request)
        if rejected is not None:
            return rejected

        gated = await self.gate_or_reject(row.id, request)
        if isinstance(gated, GenerationOutcome):
            return gated
        reservation = gated

        frame_images = self.frame_images_for(row.id)
        # frame_images and input_references are distinct fields, and when both
        # are supplied the provider honours frame_images and ignores the rest
        # (spec section 2.3), so only one is ever sent.
        references = [] if frame_images else self.input_references_for(row.id)
        params = self.wire_params(request.params)

        attempt = 0
        while True:
            try:
                async with self.client_factory(self.media_kind) as client:
                    job = await client.submit_video(
                        model=request.model,
                        prompt=request.prompt,
                        frame_images=frame_images,
                        input_references=references,
                        **params,
                    )
                break
            except RateLimitError as exc:
                if attempt >= self.retry_policy.max_retries:
                    return await self.finalise_failure(
                        gen_id=row.id,
                        reason=ErrorReason.PROVIDER_FAILED,
                        detail=f"rate limited after {attempt + 1} attempts: {exc}",
                        reservation=reservation,
                    )
                await self.clock.sleep(self.retry_policy.delay_for(attempt))
                attempt += 1
            except OpenRouterError as exc:
                return await self.finalise_failure(
                    gen_id=row.id,
                    reason=self.reason_for(exc),
                    detail=exc.message,
                    reservation=reservation,
                )

        if job.generation_id:
            self._provider_generation_ids[row.id] = job.generation_id

        # Committed BEFORE polling begins, so a crash between here and the
        # first poll still leaves a recoverable row (spec section 4.3).
        self.db.set_provider_job_id(row.id, job.id)
        self._transition(row.id, GenerationKind.VIDEO, GenerationState.SUBMITTED)

        self.attach_poller(row.id, reservation=reservation)

        return GenerationOutcome(
            generation_id=row.id,
            state=GenerationState.SUBMITTED,
            file_path=None,
            asset_id=None,
            cost=None,
            error_reason=None,
            error_detail=None,
        )

    def attach_poller(
        self, gen_id: str, *, reservation: Reservation | None
    ) -> asyncio.Task[GenerationOutcome]:
        """Spawn and register the polling task.

        Idempotent per generation: a second call for a generation already
        being polled returns the existing task, so boot reattachment can never
        double-download a paid result.
        """
        existing = self._pollers.get(gen_id)
        if existing is not None and not existing.done():
            return existing

        task = asyncio.create_task(
            self.poll_until_terminal(gen_id, reservation=reservation),
            name=f"higgshole-poll-{gen_id}",
        )
        self._pollers[gen_id] = task
        task.add_done_callback(lambda done: self._forget(gen_id, done))
        return task

    def _forget(self, gen_id: str, task: asyncio.Task[GenerationOutcome]) -> None:
        if self._pollers.get(gen_id) is task:
            self._pollers.pop(gen_id, None)

    def active_pollers(self) -> Mapping[str, asyncio.Task[GenerationOutcome]]:
        return dict(self._pollers)

    async def shutdown(self, *, timeout_s: float = 10.0) -> None:
        """Cancel every poller.

        Rows left in SUBMITTED or RUNNING are picked up by resume.py at the
        next boot, which is why cancelling here is safe rather than lossy.
        """
        tasks = list(self._pollers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)
        self._pollers.clear()
```

`attach_poller` needs something to run. Add this interim polling loop to
`VideoJobRunner` so this task goes green on its own; **Task 7 replaces its body
in full** with the terminal handling, the wall-clock ceiling and the download:

```python
    async def poll_until_terminal(
        self, gen_id: str, *, reservation: Reservation | None
    ) -> GenerationOutcome:
        """Interim: poll while the provider reports a non-terminal status.

        Task 7 replaces this with the full mapping, ceiling and download.
        """
        row = self.db.get_generation(gen_id)
        while True:
            async with self.client_factory(self.media_kind) as client:
                job = await client.get_video_job(row.provider_job_id)
            state, _reason = map_provider_status(job.status)
            if state is not GenerationState.RUNNING:
                raise NotImplementedError("terminal handling arrives in Task 7")
            await self.clock.sleep(self.settings.poll_interval_seconds)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_video_submit.py -v`

Expected: PASS — `9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/runner.py tests/jobs/test_video_submit.py
git commit -m "feat: add video submission with durable job id and poller registry"
```

---

## Task 7: Polling, the wall-clock ceiling and download

**Files:**
- Modify: `src/higgshole/jobs/runner.py` (replace `poll_until_terminal`, add `download_and_finalise`)
- Test: `tests/jobs/test_video_poll.py`

**Interfaces:**
- Consumes: `map_provider_status` (Task 3), `OpenRouterClient.get_video_job`, `OpenRouterClient.download_video`, `Clock`.
- Produces:
  - `async VideoJobRunner.poll_until_terminal(gen_id, *, reservation) -> GenerationOutcome`
  - `async VideoJobRunner.download_and_finalise(gen_id, job: VideoJob, *, reservation) -> GenerationOutcome`

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_video_poll.py`:

```python
from decimal import Decimal

import pytest

from higgshole.orclient.errors import ProviderError
from higgshole.store.db import ErrorReason, GenerationState, LedgerKind
from tests.jobs.fakes import MP4_BYTES, Harness, video_job


async def _submit_and_wait(harness, *, request=None):
    outcome = await harness.video_runner.submit(request or harness.video_request())
    task = harness.video_runner.active_pollers().get(outcome.generation_id)
    if task is None:
        return outcome, outcome
    return outcome, await task


async def test_pending_then_completed_downloads_and_completes(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        [
            video_job("job-1", "in_progress"),
            video_job("job-1", "completed", cost="0.80", urls=("https://x/y.mp4",)),
        ]
    )
    harness.client.download_results.append(MP4_BYTES)

    submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost == Decimal("0.80")
    assert (harness.paths.root / final.file_path).read_bytes() == MP4_BYTES
    assert harness.events.states_for(submitted.generation_id) == [
        "PENDING",
        "SUBMITTED",
        "RUNNING",
        "DOWNLOADING",
        "COMPLETE",
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("failed", ErrorReason.PROVIDER_FAILED),
        ("cancelled", ErrorReason.PROVIDER_CANCELLED),
        ("expired", ErrorReason.PROVIDER_EXPIRED),
    ],
)
async def test_terminal_failure_statuses_map_to_their_reasons(harness, status, reason):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(
        video_job("job-1", status, error="upstream said no")
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.FAILED
    assert final.error_reason is reason
    assert harness.client.call_names().count("download_video") == 0


async def test_an_unrecognised_status_keeps_polling(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.extend(
        [
            video_job("job-1", "reticulating"),
            video_job("job-1", "reticulating"),
            video_job("job-1", "completed", cost="0.10"),
        ]
    )
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert harness.client.call_names().count("get_video_job") == 3


async def test_the_wall_clock_ceiling_fails_the_job(tmp_path, stub_media):
    harness = Harness(tmp_path, job_timeout_minutes=1, poll_interval_seconds=5)
    try:
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(100)
        )

        _submitted, final = await _submit_and_wait(harness)

        assert final.state is GenerationState.FAILED
        assert final.error_reason is ErrorReason.TIMEOUT
        # 60s ceiling at a 5s cadence: bounded, and no test ever really slept.
        assert harness.clock.monotonic() >= 60
    finally:
        harness.db.close()


async def test_a_timeout_reverses_the_reservation(tmp_path, stub_media):
    harness = Harness(tmp_path, job_timeout_minutes=1, poll_interval_seconds=5)
    try:
        harness.client.submit_results.append(video_job("job-1", "pending"))
        harness.client.poll_results.extend(
            video_job("job-1", "in_progress") for _ in range(100)
        )

        _submitted, final = await _submit_and_wait(harness)

        assert harness.ledger_total(final.generation_id) == Decimal("0")
    finally:
        harness.db.close()


async def test_a_502_download_is_retried_then_fails(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.50"))
    harness.client.download_results.extend(
        ProviderError("upstream", status_code=502) for _ in range(5)
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.FAILED
    assert final.error_reason is ErrorReason.DOWNLOAD_FAILED
    # max_retries=2 in the harness, so three attempts in total.
    assert harness.client.call_names().count("download_video") == 3


async def test_a_download_retry_that_succeeds_completes_the_job(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.50"))
    harness.client.download_results.extend(
        [ProviderError("upstream", status_code=502), MP4_BYTES]
    )

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost == Decimal("0.50")


async def test_the_result_url_is_never_persisted(harness):
    # OpenRouter proxies from the upstream provider and publishes no retention
    # window, so a result URL must never become a durable reference
    # (spec section 2.5).
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(
        video_job("job-1", "completed", cost="0.50", urls=("https://storage/x.mp4",))
    )
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    stored = harness.db.get_generation(final.generation_id)
    assert "storage" not in (stored.file_path or "")
    assert "https://" not in str(stored.params)
    assets = harness.db.list_assets_for_generation(final.generation_id)
    assert all("https://" not in asset.file_path for asset in assets)


async def test_a_completed_job_with_null_cost_leaves_the_reservation_standing(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost=None))
    harness.client.download_results.append(MP4_BYTES)

    _submitted, final = await _submit_and_wait(harness)

    assert final.state is GenerationState.COMPLETE
    assert final.cost is None

    rows = harness.db.list_ledger_for_generation(final.generation_id)
    assert LedgerKind.REVERSAL not in [row.kind for row in rows]
    assert harness.ledger_total(final.generation_id) > Decimal("0")


async def test_a_failed_job_nets_the_ledger_to_zero(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "failed", error="nope"))

    _submitted, final = await _submit_and_wait(harness)

    assert harness.ledger_total(final.generation_id) == Decimal("0")
    kinds = [row.kind for row in harness.db.list_ledger_for_generation(final.generation_id)]
    assert LedgerKind.RESERVATION in kinds
    assert LedgerKind.REVERSAL in kinds
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_video_poll.py -v`

Expected: FAIL — `NotImplementedError: terminal handling arrives in Task 7`

- [ ] **Step 3: Implement**

Replace the interim `poll_until_terminal` on `VideoJobRunner` with these two
methods:

```python
    async def poll_until_terminal(
        self, gen_id: str, *, reservation: Reservation | None
    ) -> GenerationOutcome:
        """Poll every settings.poll_interval_seconds until terminal or the
        wall-clock ceiling.

        Status mapping (spec section 4.3):
          pending, in_progress -> RUNNING, keep polling
          completed            -> DOWNLOADING then COMPLETE, download at once
          failed               -> FAILED / PROVIDER_FAILED
          cancelled            -> FAILED / PROVIDER_CANCELLED
          expired              -> FAILED / PROVIDER_EXPIRED
          unrecognised         -> RUNNING, keep polling (spec section 2.4)

        Exceeding job_timeout_minutes is FAILED / TIMEOUT with the reservation
        reversed.
        """
        row = self.db.get_generation(gen_id)
        job_id = row.provider_job_id
        if job_id is None:
            return await self.finalise_failure(
                gen_id=gen_id,
                reason=ErrorReason.INDETERMINATE,
                detail="no provider job id was recorded for this generation.",
                reservation=reservation,
            )

        timeout_s = self.settings.job_timeout_minutes * 60
        deadline = self.clock.monotonic() + timeout_s
        announced_running = row.state is GenerationState.RUNNING
        attempt = 0

        while True:
            if self.clock.monotonic() >= deadline:
                return await self.finalise_failure(
                    gen_id=gen_id,
                    reason=ErrorReason.TIMEOUT,
                    detail=(
                        f"job {job_id} was still running after "
                        f"{self.settings.job_timeout_minutes} minutes."
                    ),
                    reservation=reservation,
                )

            try:
                async with self.client_factory(self.media_kind) as client:
                    job = await client.get_video_job(job_id)
            except OpenRouterError as exc:
                # Polling is an idempotent GET and therefore freely retryable
                # (spec section 4.4).
                if attempt >= self.retry_policy.max_retries:
                    return await self.finalise_failure(
                        gen_id=gen_id,
                        reason=ErrorReason.PROVIDER_FAILED,
                        detail=f"polling failed repeatedly: {exc}",
                        reservation=reservation,
                    )
                attempt += 1
                await self.clock.sleep(self.retry_policy.delay_for(attempt))
                continue

            attempt = 0
            state, reason = map_provider_status(job.status)

            if state is GenerationState.RUNNING:
                if not announced_running:
                    self._transition(
                        gen_id, GenerationKind.VIDEO, GenerationState.RUNNING
                    )
                    announced_running = True
                await self.clock.sleep(self.settings.poll_interval_seconds)
                continue

            if state is GenerationState.DOWNLOADING:
                return await self.download_and_finalise(
                    gen_id, job, reservation=reservation
                )

            return await self.finalise_failure(
                gen_id=gen_id,
                reason=reason or ErrorReason.PROVIDER_FAILED,
                detail=job.error or f"provider reported status {job.status!r}.",
                reservation=reservation,
            )

    async def download_and_finalise(
        self,
        gen_id: str,
        job: VideoJob,
        *,
        reservation: Reservation | None,
    ) -> GenerationOutcome:
        """Download immediately within the same task that observed `completed`.

        OpenRouter proxies from the upstream provider and publishes no
        retention window, so a result URL is never persisted as a durable
        reference (spec section 2.5). A 502 is retried with backoff up to
        settings.max_retries, then FAILED / DOWNLOAD_FAILED.
        """
        if job.generation_id:
            self._provider_generation_ids[gen_id] = job.generation_id

        self._transition(gen_id, GenerationKind.VIDEO, GenerationState.DOWNLOADING)

        attempt = 0
        while True:
            try:
                async with self.client_factory(self.media_kind) as client:
                    data = await client.download_video(job.id)
                break
            except OpenRouterError as exc:
                if attempt >= self.retry_policy.max_retries:
                    return await self.finalise_failure(
                        gen_id=gen_id,
                        reason=ErrorReason.DOWNLOAD_FAILED,
                        detail=(
                            f"download failed after {attempt + 1} attempts: {exc}. "
                            "The provider's retention window may have lapsed."
                        ),
                        reservation=reservation,
                    )
                attempt += 1
                await self.clock.sleep(self.retry_policy.delay_for(attempt))

        return await self.finalise_success(
            gen_id=gen_id,
            data=data,
            media_type="video/mp4",
            cost=job.cost,
            reservation=reservation,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_video_poll.py -v`

Expected: PASS — `12 passed` (one test is parametrized over three statuses)

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/runner.py tests/jobs/test_video_poll.py
git commit -m "feat: add video polling, wall-clock ceiling and immediate download"
```

---

## Task 8: Boot-time reattachment

**Files:**
- Create: `src/higgshole/jobs/resume.py`
- Modify: `src/higgshole/jobs/__init__.py` (public re-exports)
- Test: `tests/jobs/test_resume.py`

**Interfaces:**
- Consumes: `Database.list_generations_in_states`, `RESUMABLE_STATES`, `Ledger`, `Reservation`, `VideoJobRunner.attach_poller`, `Settings.job_timeout_minutes`.
- Produces:
  - `ResumeReport(reattached: tuple[str, ...], timed_out: tuple[str, ...], orphaned: tuple[str, ...])`
  - `async resume_pending_jobs(*, db, runner, ledger, settings) -> ResumeReport`
  - `reservation_for(ledger: Ledger, gen_id: str) -> Reservation | None` — the
    rebuilt reservation always carries `from_exact_estimate=False`, because
    `Ledger.reserve` never persists exactness.

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_resume.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from higgshole.budget.estimator import Estimate
from higgshole.jobs.resume import ResumeReport, reservation_for, resume_pending_jobs
from higgshole.store.db import (
    ErrorReason,
    GenerationKind,
    GenerationState,
    LedgerKind,
)
from tests.jobs.fakes import MP4_BYTES, video_job


def _backdate(harness, gen_id: str, *, minutes: int) -> None:
    """Rewrite created_at so the row looks older than the ceiling.

    Written straight to SQLite because no application code may rewrite a
    creation timestamp; only a test needs this.
    """
    stale = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    with harness.db.transaction() as connection:
        connection.execute(
            "UPDATE generations SET created_at = ? WHERE id = ?", (stale, gen_id)
        )


async def _submitted_row(harness, *, job_id: str = "job-1") -> str:
    """A video row parked in SUBMITTED with its poller detached, as a crash
    would leave it."""
    harness.client.submit_results.append(video_job(job_id, "pending"))
    harness.client.poll_results.extend(video_job(job_id, "in_progress") for _ in range(50))
    outcome = await harness.video_runner.submit(harness.video_request())
    await harness.video_runner.shutdown()
    return outcome.generation_id


async def test_a_submitted_video_row_is_reattached(harness):
    gen_id = await _submitted_row(harness)
    harness.client.poll_results.extend(
        video_job("job-1", "in_progress") for _ in range(50)
    )

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.reattached == (gen_id,)
    assert gen_id in harness.video_runner.active_pollers()
    await harness.video_runner.shutdown()


async def test_an_image_row_is_never_reattached(harness):
    # Image rows can never occupy SUBMITTED or RUNNING, but resume filters on
    # kind anyway as a defence against a corrupted row (spec section 4.3).
    row = harness.db.create_generation(
        project_id=harness.project.id,
        kind=GenerationKind.IMAGE,
        model="test/image-model",
        prompt="a cat",
        params={},
        state=GenerationState.PENDING,
    )
    with harness.db.transaction() as connection:
        connection.execute(
            "UPDATE generations SET state = 'RUNNING', provider_job_id = 'bogus' "
            "WHERE id = ?",
            (row.id,),
        )

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report == ResumeReport(reattached=(), timed_out=(), orphaned=())
    assert harness.video_runner.active_pollers() == {}
    assert harness.client.call_names().count("get_video_job") == 0


async def test_a_running_row_older_than_the_ceiling_is_failed_with_timeout(harness):
    gen_id = await _submitted_row(harness)
    _backdate(harness, gen_id, minutes=harness.settings.job_timeout_minutes + 5)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.timed_out == (gen_id,)
    stored = harness.db.get_generation(gen_id)
    assert stored.state is GenerationState.FAILED
    assert stored.error_reason is ErrorReason.TIMEOUT
    assert harness.video_runner.active_pollers() == {}


async def test_a_resumable_row_without_a_job_id_is_failed_as_indeterminate(harness):
    # The process died between the gate and the submit response, so the
    # submission may already have been billed.
    row = harness.db.create_generation(
        project_id=harness.project.id,
        kind=GenerationKind.VIDEO,
        model="test/video-model",
        prompt="a beach",
        params={},
        state=GenerationState.PENDING,
    )
    harness.db.set_generation_state(row.id, GenerationState.SUBMITTED)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.orphaned == (row.id,)
    stored = harness.db.get_generation(row.id)
    assert stored.state is GenerationState.FAILED
    assert stored.error_reason is ErrorReason.INDETERMINATE


async def test_reservations_are_rebuilt_from_the_ledger(harness):
    gen_id = await _submitted_row(harness)

    rebuilt = reservation_for(harness.ledger, gen_id)

    assert rebuilt is not None
    assert rebuilt.generation_id == gen_id
    assert rebuilt.amount > Decimal("0")


async def test_a_rebuilt_reservation_is_never_marked_exact(harness):
    """Ledger.reserve always writes cost_known=False, so exactness is not
    persisted and cannot be recovered at boot. Pinned as False rather than
    read back from the row, which would conflate two different booleans.
    Affects reporting only — the amount is still recovered exactly."""
    gen_id = await _submitted_row(harness)

    rebuilt = reservation_for(harness.ledger, gen_id)

    assert rebuilt.from_exact_estimate is False
    rows = harness.db.list_ledger_for_generation(gen_id)
    assert all(
        row.cost_known is False
        for row in rows
        if row.kind is LedgerKind.RESERVATION
    )


async def test_reservation_for_returns_none_once_settled(harness):
    gen_id = await _submitted_row(harness)
    reservation = reservation_for(harness.ledger, gen_id)
    await harness.gate.release(reservation, actual_cost=None, succeeded=False)

    assert reservation_for(harness.ledger, gen_id) is None


async def test_a_completed_row_is_not_reattached(harness):
    harness.client.submit_results.append(video_job("job-1", "pending"))
    harness.client.poll_results.append(video_job("job-1", "completed", cost="0.20"))
    harness.client.download_results.append(MP4_BYTES)
    outcome = await harness.video_runner.submit(harness.video_request())
    await harness.video_runner.active_pollers()[outcome.generation_id]

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )

    assert report.reattached == ()


async def test_resume_survives_a_simulated_restart_mid_flight(harness):
    """The headline guarantee: a job in flight when the process dies is picked
    up at the next boot and completes normally, with its reservation intact."""
    gen_id = await _submitted_row(harness, job_id="job-7")

    # Everything in memory is gone; only SQLite and the disk survive.
    assert harness.video_runner.active_pollers() == {}
    assert harness.db.get_generation(gen_id).state is GenerationState.SUBMITTED

    harness.client.poll_results.clear()
    harness.client.poll_results.append(video_job("job-7", "completed", cost="0.75"))
    harness.client.download_results.append(MP4_BYTES)

    report = await resume_pending_jobs(
        db=harness.db,
        runner=harness.video_runner,
        ledger=harness.ledger,
        settings=harness.settings,
    )
    outcome = await harness.video_runner.active_pollers()[gen_id]

    assert report.reattached == (gen_id,)
    assert outcome.state is GenerationState.COMPLETE
    assert (harness.paths.root / outcome.file_path).read_bytes() == MP4_BYTES
    # The reservation was rebuilt from the ledger, not from memory, so the
    # actual cost reconciles exactly once.
    assert harness.ledger_total(gen_id) == Decimal("0.75")
    kinds = [row.kind for row in harness.db.list_ledger_for_generation(gen_id)]
    assert kinds.count(LedgerKind.RESERVATION) == 1
    assert kinds.count(LedgerKind.ACTUAL) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_resume.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.jobs.resume'`

- [ ] **Step 3: Implement**

Create `src/higgshole/jobs/resume.py`:

```python
"""Boot-time poller reattachment (spec section 4.3).

Video pollers are in-process asyncio tasks. When the process stops, any job
still rendering upstream keeps rendering, and its row is left in SUBMITTED or
RUNNING. This module is what makes that survivable: at the next boot the rows
are found by state, their reservations are re-derived from the ledger, and a
poller is attached to each one.

Reservations are rebuilt from the ledger rather than remembered, so a restart
can neither leak a reservation (which would shrink the cap forever) nor
double-count one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from higgshole.budget.gate import Reservation
from higgshole.budget.ledger import Ledger
from higgshole.config import Settings
from higgshole.jobs.runner import VideoJobRunner
from higgshole.store.db import (
    RESUMABLE_STATES,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationState,
    LedgerKind,
)


@dataclass(frozen=True)
class ResumeReport:
    """Emitted at startup and shown in Settings."""

    #: Generation IDs now being polled again.
    reattached: tuple[str, ...] = ()
    #: Exceeded the wall-clock ceiling while the service was down; FAILED.
    timed_out: tuple[str, ...] = ()
    #: In a resumable state but carrying no provider job ID; unrecoverable.
    orphaned: tuple[str, ...] = ()


def reservation_for(ledger: Ledger, gen_id: str) -> Reservation | None:
    """Rebuild the in-memory Reservation for a generation, or None if settled.

    Amounts are signed and the ledger is append-only, so "still outstanding"
    is simply reservations plus reversals being positive. Summing happens in
    Python because the amounts are Decimal strings — SQLite REAL would round
    money.

    `from_exact_estimate` is always rebuilt as False. Exactness is not
    persisted: `Ledger.reserve` writes `cost_known=False` on every reservation
    row, because a reservation is a ceiling rather than an observation, so the
    column cannot tell an exactly-estimated job from a pessimistic one. Reading
    it back would conflate two different booleans. False is safe because the
    flag only affects reporting — it never changes the amount reserved, which
    is recovered exactly from the summed ledger rows.
    """
    rows = ledger.db.list_ledger_for_generation(gen_id)
    reservations = [row for row in rows if row.kind is LedgerKind.RESERVATION]
    if not reservations:
        return None

    outstanding = sum(
        (
            row.amount
            for row in rows
            if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
        ),
        Decimal("0"),
    )
    if outstanding <= Decimal("0"):
        return None

    latest = reservations[-1]
    return Reservation(
        generation_id=gen_id,
        amount=outstanding,
        # Not recoverable at boot; see the docstring. Reporting only.
        from_exact_estimate=False,
        ledger_row_id=latest.id,
    )


def _is_older_than(created_at: str, ceiling: timedelta) -> bool:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created >= ceiling


async def resume_pending_jobs(
    *,
    db: Database,
    runner: VideoJobRunner,
    ledger: Ledger,
    settings: Settings,
) -> ResumeReport:
    """Reattach pollers to video generations left mid-flight.

    Selects rows with kind='video' and state in RESUMABLE_STATES. Only video
    rows can occupy those states, but the kind filter is applied anyway as a
    defence against a corrupted row.

    A row already older than job_timeout_minutes is failed immediately with
    TIMEOUT rather than reattached. A row in a resumable state with
    provider_job_id NULL is unrecoverable — the process died between the gate
    and the submit response — and is failed with INDETERMINATE, since the
    submission may have been billed.
    """
    rows = db.list_generations_in_states(
        RESUMABLE_STATES, kind=GenerationKind.VIDEO
    )
    ceiling = timedelta(minutes=settings.job_timeout_minutes)

    reattached: list[str] = []
    timed_out: list[str] = []
    orphaned: list[str] = []

    for row in rows:
        reservation = reservation_for(ledger, row.id)

        if row.provider_job_id is None:
            db.set_generation_state(
                row.id,
                GenerationState.FAILED,
                error_reason=ErrorReason.INDETERMINATE,
                error_detail=(
                    "The service stopped between reserving budget and receiving "
                    "a job ID, so this submission may have been billed without "
                    "producing a recoverable job."
                ),
            )
            if reservation is not None:
                await runner.gate.release(
                    reservation, actual_cost=None, succeeded=False
                )
            orphaned.append(row.id)
            continue

        if _is_older_than(row.created_at, ceiling):
            db.set_generation_state(
                row.id,
                GenerationState.FAILED,
                error_reason=ErrorReason.TIMEOUT,
                error_detail=(
                    f"Job {row.provider_job_id} exceeded the "
                    f"{settings.job_timeout_minutes}-minute ceiling while the "
                    "service was not running."
                ),
            )
            if reservation is not None:
                await runner.gate.release(
                    reservation, actual_cost=None, succeeded=False
                )
            timed_out.append(row.id)
            continue

        runner.attach_poller(row.id, reservation=reservation)
        reattached.append(row.id)

    return ResumeReport(
        reattached=tuple(reattached),
        timed_out=tuple(timed_out),
        orphaned=tuple(orphaned),
    )
```

Replace `src/higgshole/jobs/__init__.py` with the public surface:

```python
"""Generation state machines and everything that orchestrates a job."""

from .clock import Clock, RealClock
from .events import EventPublisher, JobEvent, NullEventPublisher
from .references import (
    DEFAULT_MAX_DATA_URI_BYTES,
    ReferenceTooLargeError,
    ReferenceTransport,
    UnsupportedTransportError,
    build_input_references,
    build_reference,
    build_video_frame_images,
    encode_data_uri,
    video_references_supported,
)
from .resume import ResumeReport, reservation_for, resume_pending_jobs
from .runner import (
    GenerationOutcome,
    GenerationRequest,
    ImageJobRunner,
    JobRunner,
    RetryPolicy,
    VideoJobRunner,
    map_provider_status,
)

__all__ = [
    "DEFAULT_MAX_DATA_URI_BYTES",
    "Clock",
    "EventPublisher",
    "GenerationOutcome",
    "GenerationRequest",
    "ImageJobRunner",
    "JobEvent",
    "JobRunner",
    "NullEventPublisher",
    "RealClock",
    "ReferenceTooLargeError",
    "ReferenceTransport",
    "ResumeReport",
    "RetryPolicy",
    "UnsupportedTransportError",
    "VideoJobRunner",
    "build_input_references",
    "build_reference",
    "build_video_frame_images",
    "encode_data_uri",
    "map_provider_status",
    "reservation_for",
    "resume_pending_jobs",
    "video_references_supported",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/test_resume.py -v`

Expected: PASS — `9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/jobs/resume.py src/higgshole/jobs/__init__.py tests/jobs/test_resume.py
git commit -m "feat: reattach video pollers to in-flight jobs at boot"
```

---

## Task 9: Cross-cutting invariants

**Files:**
- Modify: `src/higgshole/store/db.py` (`count_in_flight` gains `exclude_generation_id`)
- Modify: `src/higgshole/budget/gate.py` (`acquire` excludes the row it is gating)
- Test: `tests/jobs/test_invariants.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `Database.count_in_flight(exclude_generation_id: str | None = None) -> int`
  - Removes the private `BudgetGate._in_flight_excluding`, whose Python-side
    subtraction the SQL exclusion replaces.
  - No new public symbol in `jobs/`; this task proves the guarantees the spec names.

**Why the change to Plan 2.** The runner inserts the generation row in
`PENDING` *before* the gate, because a cap rejection must be recorded as a
`REJECTED` row against a real generation. That row is itself non-terminal, so a
naive `count_in_flight()` counts the very job being gated and the ceiling is
off by one. Plan 2 already compensates, but in Python: `BudgetGate` calls a
private `_in_flight_excluding` helper that re-reads the row and subtracts 1.
That works for one row and does not compose — the count and the correction are
two separate reads, and the correction is easy to duplicate. This task moves
the exclusion into the query and deletes the helper, so exactly one exclusion
exists in exactly one place. The ordering rule is unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/jobs/test_invariants.py`:

```python
import asyncio
from decimal import Decimal
from pathlib import Path

import higgshole.jobs as jobs_package
from higgshole.orclient.errors import ProviderError
from higgshole.store.db import ErrorReason, GenerationState, LedgerKind
from tests.jobs.fakes import Harness, video_job


async def test_concurrent_submissions_cannot_exceed_the_daily_cap(tmp_path, stub_media):
    """Spec section 3.3: without a serialized gate, ten submissions in one
    second would each observe the same remaining balance."""
    harness = Harness(tmp_path, daily_cap_usd=Decimal("3.00"), max_in_flight=10)
    try:
        # The video model is token-priced, so no exact estimate exists and each
        # job reserves the pessimistic ceiling of 2.00. A 3.00 cap admits one.
        for index in range(3):
            harness.client.submit_results.append(video_job(f"job-{index}", "pending"))
        harness.client.poll_results.extend(
            video_job("job-0", "in_progress") for _ in range(200)
        )

        outcomes = await asyncio.gather(
            *(harness.video_runner.submit(harness.video_request()) for _ in range(3))
        )

        submitted = [o for o in outcomes if o.state is GenerationState.SUBMITTED]
        rejected = [o for o in outcomes if o.state is GenerationState.REJECTED]

        assert len(submitted) == 1
        assert len(rejected) == 2
        assert all(o.error_reason is ErrorReason.CAP_EXCEEDED for o in rejected)

        # Total reserved never exceeded the cap.
        outstanding = harness.ledger.outstanding_reservations()
        assert outstanding <= Decimal("3.00")
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_concurrent_submissions_respect_the_in_flight_ceiling(
    tmp_path, stub_media
):
    harness = Harness(tmp_path, daily_cap_usd=None, max_in_flight=2)
    try:
        for index in range(4):
            harness.client.submit_results.append(video_job(f"job-{index}", "pending"))
        harness.client.poll_results.extend(
            video_job("job-0", "in_progress") for _ in range(200)
        )

        outcomes = await asyncio.gather(
            *(harness.video_runner.submit(harness.video_request()) for _ in range(4))
        )

        submitted = [o for o in outcomes if o.state is GenerationState.SUBMITTED]
        rejected = [o for o in outcomes if o.state is GenerationState.REJECTED]

        assert len(submitted) == 2
        assert all(o.error_reason is ErrorReason.IN_FLIGHT_LIMIT for o in rejected)
    finally:
        await harness.video_runner.shutdown()
        harness.db.close()


async def test_a_failed_job_reservation_nets_to_zero(harness):
    harness.client.image_results.append(
        ProviderError("upstream exploded", status_code=500)
    )

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.FAILED
    rows = harness.db.list_ledger_for_generation(outcome.generation_id)
    assert [row.kind for row in rows].count(LedgerKind.REVERSAL) == 1
    assert harness.ledger_total(outcome.generation_id) == Decimal("0")


async def test_a_completed_job_with_unknown_cost_marks_the_day_a_lower_bound(harness):
    # Spec section 3.4: the cap over-counts rather than under-counts, and zero
    # is never recorded as the charge.
    harness.client.image_results.append(harness.image_result(cost=None))

    outcome = await harness.image_runner.run(harness.image_request())

    assert outcome.state is GenerationState.COMPLETE
    spend = harness.ledger.spend_for_day()
    assert spend.is_lower_bound is True
    assert spend.total > Decimal("0")


async def test_the_gated_row_is_excluded_exactly_once(harness):
    """Pins the arithmetic: the gate must not subtract its own row a second
    time on top of the SQL exclusion, or the ceiling would be off by one."""
    harness.client.submit_results.append(video_job("job-0", "pending"))
    harness.client.poll_results.extend(
        video_job("job-0", "in_progress") for _ in range(200)
    )
    try:
        outcome = await harness.video_runner.submit(harness.video_request())
        assert outcome.state is GenerationState.SUBMITTED

        # One non-terminal row exists. Excluding it yields zero, never minus one.
        assert harness.db.count_in_flight() == 1
        assert (
            harness.db.count_in_flight(exclude_generation_id=outcome.generation_id) == 0
        )
        assert harness.db.count_in_flight(exclude_generation_id="no-such-id") == 1
    finally:
        await harness.video_runner.shutdown()


def test_jobs_package_never_imports_web():
    # Dependency direction is one-way (spec section 4.1): web imports jobs.
    package_dir = Path(jobs_package.__file__).parent
    offenders = [
        source.name
        for source in package_dir.glob("*.py")
        if "higgshole.web" in source.read_text(encoding="utf-8")
    ]

    assert offenders == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/jobs/test_invariants.py -v`

Expected: FAIL — `test_concurrent_submissions_respect_the_in_flight_ceiling` reports `assert 0 == 2`, because every concurrent request counts the other requests' own `PENDING` rows against the ceiling.

- [ ] **Step 3: Implement**

In `src/higgshole/store/db.py`, replace `count_in_flight` with:

```python
    def count_in_flight(self, exclude_generation_id: str | None = None) -> int:
        """Generations in any non-terminal state.

        Read inside the budget gate's lock (spec section 3.3). The gate passes
        the generation it is currently deciding on, because that row is
        already inserted in PENDING — a job must not count itself towards the
        ceiling, and with concurrent submissions it would otherwise see every
        other pending request and refuse them all.
        """
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        sql = (
            f"SELECT COUNT(*) FROM generations WHERE state NOT IN ({placeholders})"
        )
        parameters: list[object] = [str(state) for state in TERMINAL_STATES]
        if exclude_generation_id is not None:
            sql += " AND id != ?"
            parameters.append(exclude_generation_id)

        # Plan 2's Database names its connection attribute `_conn`.
        row = self._conn.execute(sql, parameters).fetchone()
        return int(row[0])
```

In `src/higgshole/budget/gate.py`, Plan 2 already excludes the gated row, but in
Python and only after the fact, via a private helper. Delete that helper
entirely — leaving it in place would be dead code at best, and a second
subtraction on top of the new SQL exclusion at worst:

```python
    def _in_flight_excluding(self, generation_id: str) -> int:
        """In-flight count that does not include the row being gated.

        The generation is inserted as PENDING before the gate runs, so it is
        already counted; subtracting it here keeps max_in_flight=3 meaning
        three concurrent jobs rather than two.
        """
        count = self._db.count_in_flight()
        own = self._db.get_generation(generation_id)
        if own is not None and own.state not in TERMINAL_STATES:
            count -= 1
        return count
```

Then, inside `acquire`, replace Plan 2's line

```python
            in_flight = self._in_flight_excluding(generation_id)
```

with the exclusion pushed into the query, so the count is taken once, under the
lock, with no second adjustment anywhere:

```python
            # Excluded in SQL, not subtracted afterwards: exactly one exclusion
            # happens, so max_in_flight=3 still means three concurrent jobs.
            in_flight = self._db.count_in_flight(exclude_generation_id=generation_id)
```

`TERMINAL_STATES` was imported by `gate.py` only for the deleted helper, so
narrow its import to `from higgshole.store.db import Database` or ruff will flag
the unused name.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/jobs/ -v`

Expected: PASS — the whole package, `84 passed` (references 9, events 5, status mapping 11, runner base 11, image runner 12, video submit 9, video poll 12, resume 9, invariants 6)

- [ ] **Step 5: Run the whole suite and lint, then commit**

Run: `uv run pytest -q && uv run ruff check .`

Expected: all tests pass, `All checks passed!`

```bash
git add src/higgshole/store/db.py src/higgshole/budget/gate.py tests/jobs/test_invariants.py
git commit -m "fix: exclude the gated generation from the in-flight count"
```

---

## Definition of done

- [ ] `uv run pytest -q` passes with the network guard active; no test makes a real request or costs money
- [ ] `uv run ruff check .` is clean
- [ ] `jobs/` imports nothing from `web/`, and Task 9 asserts it
- [ ] Both state machines are implemented separately: image never enters `SUBMITTED`, `RUNNING` or `DOWNLOADING`
- [ ] The full provider status table from spec §4.3 is covered, including `cancelled` → `PROVIDER_CANCELLED`, `expired` → `PROVIDER_EXPIRED`, and an unrecognised status that keeps polling
- [ ] Ordering is local validation → budget gate → dispatch, proved by a test asserting the provider is never called on a rejection
- [ ] The provider job ID is written to SQLite before the first poll
- [ ] A submit is never blindly retried; only 429-before-dispatch, polling and download are
- [ ] The wall-clock ceiling fails a stuck job and reverses its reservation, with no test sleeping for a real duration
- [ ] A download starts within the task that observed `completed`, and no result URL is persisted
- [ ] A failed job's reservation nets to zero; a completed job with a null cost leaves the reservation standing and marks the day a lower bound
- [ ] A job in flight across a simulated restart is reattached and completes, with its reservation rebuilt from the ledger
- [ ] An image row is never reattached at boot
- [ ] No committed file contains a personal name, employer name, machine-specific path, or API key, and no cost is ever fabricated

---

## Contract additions

The frozen contract did not cover the following. Each is added in the most
consistent style available and is used only by this plan unless noted.

1. **`jobs/events.py` owns `JobEvent`.** The contract places `JobEvent` and
   `EventBus` in `web/sse.py` (§10.4) while `JobRunner.__init__` takes
   `events: EventBus` (§9.2) — which would make `jobs/` import `web/` and
   invert the dependency direction of spec §4.1. `JobEvent` (including
   `to_sse`) therefore lives in `jobs/events.py`, alongside an
   `EventPublisher` Protocol (`publish(event: JobEvent) -> None`) and a
   `NullEventPublisher`. **Plan 4** should re-export `JobEvent` from
   `web/sse.py` unchanged and make `EventBus` satisfy `EventPublisher`; §10.4's
   surface is then met verbatim.

2. **`jobs/clock.py`: `Clock` protocol and `RealClock`.** The contract fixes no
   time source, and the wall-clock ceiling (§9.2) and backoff (§4.4) cannot be
   tested without one. `JobRunner.__init__` gains two optional keyword
   arguments, `clock: Clock | None = None` and
   `retry_policy: RetryPolicy | None = None`, both defaulting to the
   production behaviour, so every contract-specified call site is unaffected.

3. **`JobRunner.estimate(request) -> Estimate`**, plus the helpers
   `gate_or_reject`, `validate_or_reject`, `reason_for`, `input_references_for`,
   `frame_images_for` and `wire_params`. The contract specifies `validate` and
   the gate but not the step that produces the `Estimate` the gate consumes.

4. **`Database.count_in_flight` gains `exclude_generation_id: str | None = None`,
   and `BudgetGate.acquire` passes it.** Required for correctness, not
   convenience: the generation being gated is already inserted in `PENDING`, so
   without the exclusion a job counts itself and concurrent submissions refuse
   one another. Plan 2 handles this with a private `BudgetGate`
   `_in_flight_excluding` helper that subtracts 1 after the fact; Task 9 moves
   the exclusion into SQL and **deletes that helper**, so the row is never
   excluded twice. Implemented in full in Task 9. **This replaces Plan 2's
   `count_in_flight`, deletes `_in_flight_excluding`, and changes its one call
   site in `acquire`.**

5. **`Ledger.db` is a public attribute.** `reservation_for(ledger, gen_id)`
   (§9.3) must read that generation's ledger rows, and the contract's `Ledger`
   exposes only whole-ledger aggregates. The alternative — widening
   `reservation_for`'s signature to take a `Database` — would contradict §9.3.

6. **Database calls are made directly, not through `anyio.to_thread.run_sync`.**
   The contract's §5.3 note suggests wrapping; a `sqlite3` connection is bound
   to its creating thread by default, so wrapping would require reopening the
   connection per call to buy nothing on statements that take microseconds.
   Documented in `JobRunner`'s docstring.

7. **`GenerationRequest.params` carries the video-specific keys** `duration`,
   `resolution`, `aspect_ratio`, `size`, `generate_audio` and `seed`, and the
   image-specific keys `quality`, `output_format`, `background`, `size`,
   `resolution`, `aspect_ratio` and `seed` — matching the `GenerateImageIn` /
   `GenerateVideoIn` field names in §10.3, so `web/api.py` can build a request
   without renaming anything. `n` is stripped before dispatch and rejected by
   validation (spec §5.5).
