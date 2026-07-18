# HiggsHole Plan 2 — Storage, Metadata & Budget

> **How to execute this plan:** work through it strictly task by task, in order.
> Each task is self-contained and ends with a passing test suite and a commit,
> so it is a natural review checkpoint — do not start the next task until the
> current one is green. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Every task follows the same cycle: write a failing test, run it to confirm it
> fails for the reason you expect, write the minimal implementation, confirm it
> passes, commit. Do not write implementation before its test.

**Goal:** Build every component that touches disk or the database — path allocation, atomic writes, the SQLite schema, media metadata and thumbnails — plus the catalogue cache and the whole cost-control stack (estimator, ledger, reservation gate).

**Architecture:** `store/` owns the filesystem and the database and never calls OpenRouter; `catalog/` is the only component allowed to hold the model catalogue, because `orclient` cannot persist and `store` cannot fetch; `budget/` layers an estimator, an append-only signed-amount ledger and a serialized reservation gate on top of `store`. Dependency direction is one-way: `budget` → `catalog` → `store` → nothing.

**Tech Stack:** Python 3.12+, `uv`, `sqlite3` (stdlib), `Pillow`, `ffmpeg`/`ffprobe` (external binaries), `anyio`, `pytest`, `pytest-asyncio`.

**Source specification:** docs/specs/2026-07-18-higgshole-design.md

**Depends on:** Plan 1

## Global Constraints

- **Python 3.12+.** `StrEnum` and PEP 604 unions throughout.
- **Public repository.** No committed file may contain a personal name, an employer name, a machine-specific absolute path, or an API key.
- **`store/` never calls OpenRouter.** It imports nothing from `higgshole.orclient`. Task 8 enforces this with a test.
- **No test may make a real network request or cost money.** The autouse `_forbid_real_network` fixture from Plan 1 Task 10 is inherited by every new test package. New test directories need an `__init__.py`; they must **not** add a second `conftest.py` that overrides the guard.
- **Never fabricate a cost.** Every monetary value crossing a module boundary is `Decimal | None`. `None` means unknown; `0` means a genuine zero. Never `float`.
- **Terminal job statuses are exactly** `completed`, `failed`, `cancelled`, `expired`. Any unrecognised status is non-terminal — keep polling.
- **`spend_ledger` is append-only.** No `UPDATE` and no `DELETE` except the `ON DELETE CASCADE` in the schema.
- **`spend_ledger.amount` is `TEXT`** holding a signed `Decimal` literal. Window totals are summed in Python, never with SQL `SUM()`.
- **Path containment is checked on every media read**, via `MediaPaths.resolve_within_root`. There is no unguarded read path.
- **Timestamps** are UTC ISO-8601 strings with an explicit offset, produced by exactly one helper: `store.db.utc_now_iso`.
- **Identifiers** are 12 lowercase hex characters (48 bits), with collision retry against the `UNIQUE` constraint.
- **ffmpeg/ffprobe tests generate their own fixture media at test time** and skip when the binaries are absent. No media file is committed to the repository.
- Commit after every task. Conventional commit prefixes (`feat:`, `test:`, `chore:`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/higgshole/store/__init__.py` | Public re-exports for the storage package |
| `src/higgshole/store/paths.py` | Media-root layout, slugs, identifiers, traversal containment |
| `src/higgshole/store/files.py` | Atomic `.part`+rename writes, sidecar JSON, hashing |
| `src/higgshole/store/db.py` | Shared enums, the eight-table schema, every query |
| `src/higgshole/store/metadata.py` | Pillow/ffprobe probing, parameter embedding, thumbnails |
| `src/higgshole/catalog/cache.py` | Catalogue persistence, TTL refresh, lazy image pricing |
| `src/higgshole/budget/__init__.py` | Public re-exports for the budget package |
| `src/higgshole/budget/estimator.py` | Cost estimation from `pricing_skus` and image line items |
| `src/higgshole/budget/ledger.py` | Append-only signed-amount ledger, UTC-day cap window |
| `src/higgshole/budget/gate.py` | Serialized reservation gate and in-flight ceiling |
| `tests/store/__init__.py` | Test package marker |
| `tests/store/test_paths.py` | Layout, slugs, IDs, traversal |
| `tests/store/test_files.py` | Atomic writes and sidecars |
| `tests/store/test_db_schema.py` | Migration, projects, settings |
| `tests/store/test_db_generations.py` | Generations, assets, lineage |
| `tests/store/test_db_catalog_ledger.py` | Ledger rows, catalogue and pricing storage |
| `tests/store/test_metadata_probe.py` | Probing and MIME helpers |
| `tests/store/test_metadata_embed.py` | Embedding and thumbnails |
| `tests/store/test_package.py` | Re-exports and the no-network-import invariant |
| `tests/catalog/test_cache.py` | Catalogue cache and TTL behaviour |
| `tests/budget/__init__.py` | Test package marker |
| `tests/budget/test_estimator_video.py` | Video SKU resolution |
| `tests/budget/test_estimator_image.py` | Image line-item pricing |
| `tests/budget/test_ledger.py` | Ledger arithmetic and the cap window |
| `tests/budget/test_gate.py` | Reservation gate and ceilings |

---

## Task 1: Path allocation, slugs and identifiers

**Files:**
- Create: `src/higgshole/store/__init__.py` (empty for now; Task 8 populates it)
- Create: `src/higgshole/store/paths.py`
- Create: `tests/store/__init__.py` (empty)
- Test: `tests/store/test_paths.py`

**Interfaces:**
- Consumes: `higgshole.config.Settings` (Plan 1 Task 2).
- Produces: `ID_LENGTH: int`, `SLUG_MAX_LENGTH: int`, `TIMESTAMP_FORMAT: str`, `DEFAULT_PROJECT_SLUG: str`, `new_id() -> str`, `is_valid_id(candidate: str) -> bool`, `slugify(text: str, *, max_length: int = SLUG_MAX_LENGTH) -> str`, `project_slug(name: str) -> str`, `timestamp_prefix(when: datetime | None = None) -> str`, `media_filename(*, timestamp: str, gen_id: str, slug: str, ext: str) -> str`, `AllocatedPath`, `MediaPaths`, `PathTraversalError`.
- Note: `MediaPaths.allocate_output` takes `kind: GenerationKind`, which does not exist until Task 3. To keep tasks strictly ordered, this task types the parameter as `str` and Task 3 leaves it unchanged — `GenerationKind` is a `StrEnum`, so `GenerationKind.IMAGE == "image"` and the annotation widens rather than breaks. The docstring records this.

- [ ] **Step 1: Write the failing test**

Create an empty `tests/store/__init__.py`, then create `tests/store/test_paths.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from higgshole.config import Settings
from higgshole.store.paths import (
    DEFAULT_PROJECT_SLUG,
    ID_LENGTH,
    MediaPaths,
    PathTraversalError,
    is_valid_id,
    media_filename,
    new_id,
    project_slug,
    slugify,
    timestamp_prefix,
)

WHEN = datetime(2026, 7, 18, 14, 30, 22, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path):
    return MediaPaths(tmp_path / "media")


def test_new_id_is_twelve_lowercase_hex():
    value = new_id()

    assert len(value) == ID_LENGTH == 12
    assert all(c in "0123456789abcdef" for c in value)


def test_new_ids_are_distinct():
    assert len({new_id() for _ in range(500)}) == 500


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("a3f21c9d4e07", True),
        ("A3F21C9D4E07", False),  # uppercase is not the canonical form
        ("a3f21c9d4e0", False),  # eleven characters
        ("a3f21c9d4e077", False),  # thirteen characters
        ("../../../etc", False),  # a crafted path segment
        ("", False),
    ],
)
def test_is_valid_id(candidate, expected):
    assert is_valid_id(candidate) is expected


def test_slugify_normalises_and_collapses():
    assert slugify("Neon City Street  at Night, Rain!") == "neon-city-street-at-night-rain"


def test_slugify_strips_diacritics_via_nfkd():
    assert slugify("café über straße") == "cafe-uber-strae"


def test_slugify_truncates_to_sixty_characters():
    result = slugify("word " * 60)

    assert len(result) <= 60
    assert not result.endswith("-")


def test_slugify_returns_empty_when_nothing_survives():
    assert slugify("!!! ??? ***") == ""


def test_project_slug_falls_back_to_unsorted():
    assert project_slug("!!!") == DEFAULT_PROJECT_SLUG
    assert project_slug("My Project") == "my-project"


def test_timestamp_prefix_formats_utc():
    assert timestamp_prefix(WHEN) == "20260718-143022"


def test_media_filename_includes_slug():
    name = media_filename(
        timestamp="20260718-143022", gen_id="a3f21c9d4e07", slug="neon-city", ext="png"
    )

    assert name == "20260718-143022_a3f21c9d4e07_neon-city.png"


def test_media_filename_omits_empty_slug():
    name = media_filename(
        timestamp="20260718-143022", gen_id="a3f21c9d4e07", slug="", ext="png"
    )

    assert name == "20260718-143022_a3f21c9d4e07.png"


def test_ensure_project_tree_creates_all_directories(paths):
    project = paths.ensure_project_tree("demo")

    assert (project / "images").is_dir()
    assert (project / "videos").is_dir()
    assert (project / "uploads").is_dir()
    assert paths.thumbs_dir("demo").is_dir()
    # Idempotent.
    assert paths.ensure_project_tree("demo") == project


@pytest.mark.parametrize(
    ("kind", "folder", "ext"),
    [("image", "images", "png"), ("video", "videos", "mp4")],
)
def test_allocate_output_places_images_and_videos_correctly(paths, kind, folder, ext):
    allocated = paths.allocate_output(
        project_slug="demo",
        kind=kind,
        gen_id="a3f21c9d4e07",
        prompt="Neon City Street",
        ext=ext,
        when=WHEN,
    )

    assert allocated.media_path.parent == paths.root / "projects" / "demo" / folder
    assert allocated.media_path.name == f"20260718-143022_a3f21c9d4e07_neon-city-street.{ext}"
    assert allocated.relative_media_path == Path(
        f"projects/demo/{folder}/20260718-143022_a3f21c9d4e07_neon-city-street.{ext}"
    )


def test_allocate_output_returns_sidecar_and_part_paths(paths):
    allocated = paths.allocate_output(
        project_slug="demo",
        kind="image",
        gen_id="a3f21c9d4e07",
        prompt="cat",
        ext="png",
        when=WHEN,
    )

    assert allocated.sidecar_path == allocated.media_path.with_suffix(".json")
    assert allocated.part_path.name == allocated.media_path.name + ".part"
    assert allocated.media_path.parent.is_dir()


def test_allocate_upload_lands_in_uploads(paths):
    allocated = paths.allocate_upload(
        project_slug="demo",
        asset_id="0c118b4e77aa",
        original_name="My Reference Photo.PNG",
        ext="png",
        when=WHEN,
    )

    assert allocated.media_path.parent == paths.uploads_dir("demo")
    assert allocated.media_path.name == (
        "20260718-143022_0c118b4e77aa_my-reference-photo-png.png"
    )


def test_thumb_and_poster_paths_are_sharded_by_project(paths):
    thumb = paths.thumb_path(project_slug="demo", gen_id="a3f21c9d4e07")
    poster = paths.poster_path(project_slug="demo", gen_id="a3f21c9d4e07")

    assert thumb == paths.root / "thumbs" / "demo" / "a3f21c9d4e07.webp"
    assert poster == paths.root / "thumbs" / "demo" / "a3f21c9d4e07_poster.webp"


def test_resolve_within_root_accepts_a_contained_path(paths):
    paths.ensure_project_tree("demo")
    target = paths.images_dir("demo") / "x.png"
    target.write_bytes(b"x")

    assert paths.resolve_within_root("projects/demo/images/x.png") == target.resolve()


@pytest.mark.parametrize(
    "attempt",
    [
        "../../etc/passwd",
        "projects/demo/../../../../etc/passwd",
        "/etc/passwd",
    ],
)
def test_resolve_within_root_rejects_traversal(paths, attempt):
    with pytest.raises(PathTraversalError):
        paths.resolve_within_root(attempt)


def test_is_within_root_rejects_a_sibling_directory(tmp_path):
    paths = MediaPaths(tmp_path / "media")

    assert paths.is_within_root(tmp_path / "media" / "a" / "b") is True
    assert paths.is_within_root(tmp_path / "media-elsewhere" / "b") is False


def test_from_settings_uses_the_media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_MEDIA_ROOT", str(tmp_path / "custom"))

    assert MediaPaths.from_settings(Settings()).root == (tmp_path / "custom").resolve()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_paths.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.store'`

- [ ] **Step 3: Implement**

Create an empty `src/higgshole/store/__init__.py`.

Create `src/higgshole/store/paths.py`:

```python
"""Everything under HIGGSHOLE_MEDIA_ROOT is addressed from here.

Nothing else in the codebase builds a media path. Concentrating path
construction in one place is what makes the containment check in
``resolve_within_root`` a complete guarantee rather than a convention: there
is no second code path that could open a file without passing through it
(spec section 7).
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from higgshole.config import Settings

#: 12 hex characters = 48 bits. Spec section 5.1: the earlier 6-character form
#: reached 50% collision probability at roughly 4,800 items, inside a year of
#: routine use. Uniqueness is still enforced by the database, not by chance.
ID_LENGTH: int = 12

SLUG_MAX_LENGTH: int = 60
TIMESTAMP_FORMAT: str = "%Y%m%d-%H%M%S"
DEFAULT_PROJECT_SLUG: str = "unsorted"

_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PathTraversalError(ValueError):
    """A requested path resolved outside the media root."""


def new_id() -> str:
    """A fresh 12-lowercase-hex identifier.

    Uniqueness is enforced by the UNIQUE constraint on generations.id; the
    caller retries on collision (spec section 5.1).
    """
    return secrets.token_hex(ID_LENGTH // 2)


def is_valid_id(candidate: str) -> bool:
    """Exactly 12 lowercase hex characters.

    Used to reject crafted path segments before they reach the filesystem
    (spec section 7).
    """
    return bool(_ID_RE.match(candidate))


def slugify(text: str, *, max_length: int = SLUG_MAX_LENGTH) -> str:
    """NFKD-normalise, lowercase, collapse non-[a-z0-9] runs to '-', trim.

    Returns "" when nothing survives; callers must omit the segment and its
    separator rather than emit a trailing underscore (spec section 5.1). The
    60-character bound keeps the whole filename well inside the 255-byte ext4
    limit even with a timestamp and an identifier prepended.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    collapsed = _NON_SLUG_RE.sub("-", ascii_only).strip("-")
    return collapsed[:max_length].strip("-")


def project_slug(name: str) -> str:
    """Slugify a project name, falling back to DEFAULT_PROJECT_SLUG.

    An agent must never be able to create a project it cannot then address.
    """
    return slugify(name) or DEFAULT_PROJECT_SLUG


def timestamp_prefix(when: datetime | None = None) -> str:
    """Format a UTC datetime as YYYYMMDD-HHMMSS. Defaults to now."""
    moment = when or datetime.now(UTC)
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def media_filename(*, timestamp: str, gen_id: str, slug: str, ext: str) -> str:
    """'{timestamp}_{id}_{slug}.{ext}', or '{timestamp}_{id}.{ext}' when slug
    is empty. `ext` is given without a leading dot.
    """
    stem = f"{timestamp}_{gen_id}_{slug}" if slug else f"{timestamp}_{gen_id}"
    return f"{stem}.{ext}"


@dataclass(frozen=True)
class AllocatedPath:
    """Where one media file and its sidecar will be written."""

    media_path: Path
    sidecar_path: Path
    part_path: Path
    relative_media_path: Path


class MediaPaths:
    """Owns every path under HIGGSHOLE_MEDIA_ROOT. Nothing else builds paths."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()

    @classmethod
    def from_settings(cls, settings: Settings) -> MediaPaths:
        return cls(settings.media_root)

    @property
    def root(self) -> Path:
        return self._root

    # -- directories ----------------------------------------------------

    def project_dir(self, slug: str) -> Path:
        return self._root / "projects" / slug

    def images_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "images"

    def videos_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "videos"

    def uploads_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "uploads"

    def thumbs_dir(self, slug: str) -> Path:
        return self._root / "thumbs" / slug

    def ensure_project_tree(self, slug: str) -> Path:
        """Create projects/<slug>/{images,videos,uploads} and thumbs/<slug>.

        Idempotent. Returns the project directory.
        """
        for directory in (
            self.images_dir(slug),
            self.videos_dir(slug),
            self.uploads_dir(slug),
            self.thumbs_dir(slug),
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self.project_dir(slug)

    # -- files ----------------------------------------------------------

    def _allocate(self, directory: Path, filename: str) -> AllocatedPath:
        directory.mkdir(parents=True, exist_ok=True)
        media_path = directory / filename
        return AllocatedPath(
            media_path=media_path,
            sidecar_path=self.sidecar_path(media_path),
            part_path=media_path.with_name(media_path.name + ".part"),
            relative_media_path=media_path.relative_to(self._root),
        )

    def allocate_output(
        self,
        *,
        project_slug: str,
        kind: str,
        gen_id: str,
        prompt: str,
        ext: str,
        when: datetime | None = None,
    ) -> AllocatedPath:
        """Reserve the output path for a generation.

        Creates directories but writes nothing. ``kind`` is annotated ``str``
        rather than ``GenerationKind`` only because that enum lives in db.py,
        which paths.py must not import; ``GenerationKind`` is a ``StrEnum``, so
        callers pass the enum member directly and it compares equal.
        """
        directory = self.images_dir(project_slug) if kind == "image" else self.videos_dir(
            project_slug
        )
        filename = media_filename(
            timestamp=timestamp_prefix(when),
            gen_id=gen_id,
            slug=slugify(prompt),
            ext=ext,
        )
        return self._allocate(directory, filename)

    def allocate_upload(
        self,
        *,
        project_slug: str,
        asset_id: str,
        original_name: str,
        ext: str,
        when: datetime | None = None,
    ) -> AllocatedPath:
        """Reserve a path under projects/<slug>/uploads/ for an ingested file.

        The original name is slugified rather than preserved: an operator- or
        agent-supplied filename is untrusted input, and slugifying it removes
        separators, control characters and case ambiguity in one step.
        """
        filename = media_filename(
            timestamp=timestamp_prefix(when),
            gen_id=asset_id,
            slug=slugify(original_name),
            ext=ext,
        )
        return self._allocate(self.uploads_dir(project_slug), filename)

    def thumb_path(self, *, project_slug: str, gen_id: str) -> Path:
        """thumbs/<slug>/<gen_id>.webp.

        Sharded by project so a rename or delete touches exactly one directory
        and cannot orphan or overwrite another project's files (spec 5.1).
        """
        return self.thumbs_dir(project_slug) / f"{gen_id}.webp"

    def poster_path(self, *, project_slug: str, gen_id: str) -> Path:
        """thumbs/<slug>/<gen_id>_poster.webp — the video poster frame."""
        return self.thumbs_dir(project_slug) / f"{gen_id}_poster.webp"

    def sidecar_path(self, media_path: Path) -> Path:
        return media_path.with_suffix(".json")

    # -- containment (spec section 7, unconditional) --------------------

    def is_within_root(self, candidate: Path) -> bool:
        """Whether a fully resolved path lies inside the media root."""
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            return False
        return resolved == self._root or resolved.is_relative_to(self._root)

    def resolve_within_root(self, relative: str | Path) -> Path:
        """Resolve a caller-supplied relative path and assert containment.

        Raises PathTraversalError on escape. Every media-serving endpoint calls
        this before opening a file; there is no unguarded read path. An
        absolute input is refused outright rather than silently reinterpreted,
        because ``root / "/etc/passwd"`` evaluates to ``/etc/passwd``.
        """
        candidate = Path(relative)
        if candidate.is_absolute():
            raise PathTraversalError(f"absolute paths are not accepted: {relative}")

        resolved = (self._root / candidate).resolve()
        if not self.is_within_root(resolved):
            raise PathTraversalError(f"path escapes the media root: {relative}")
        return resolved
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_paths.py -v`

Expected: PASS — `28 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/__init__.py src/higgshole/store/paths.py tests/store/__init__.py tests/store/test_paths.py
git commit -m "feat: add media path allocation, slugs and traversal containment"
```

---

## Task 2: Atomic writes and sidecars

**Files:**
- Create: `src/higgshole/store/files.py`
- Test: `tests/store/test_files.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SIDECAR_VERSION: int`, `atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None`, `atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None`, `part_file(path: Path) -> Iterator[BinaryIO]` (a context manager), `discard_part(path: Path) -> None`, `write_sidecar(sidecar_path: Path, payload: dict[str, Any]) -> None`, `read_sidecar(sidecar_path: Path) -> dict[str, Any]`, `iter_sidecars(root: Path) -> Iterator[Path]`, `delete_quietly(path: Path) -> bool`, `file_size(path: Path) -> int`, `sha256_of(path: Path) -> str`, `SidecarError`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_files.py`:

```python
import hashlib
import json
import os

import pytest

from higgshole.store.files import (
    SIDECAR_VERSION,
    SidecarError,
    atomic_write_bytes,
    atomic_write_text,
    delete_quietly,
    discard_part,
    file_size,
    iter_sidecars,
    part_file,
    read_sidecar,
    sha256_of,
    write_sidecar,
)

SIDECAR = {
    "sidecar_version": SIDECAR_VERSION,
    "id": "a3f21c9d4e07",
    "kind": "image",
    "project_slug": "unsorted",
    "model": "openai/gpt-image-2",
    "prompt": "neon city street at night, rain",
    "params": {"aspect_ratio": "16:9", "quality": "high", "seed": 7},
    "inputs": [],
    "provider": {"job_id": None, "generation_id": "gen-01J8XYZ"},
    "media": {
        "relative_path": "projects/unsorted/images/x.png",
        "mime_type": "image/png",
        "bytes": 1843200,
        "width": 1920,
        "height": 1080,
        "duration_s": None,
    },
    "cost": {"amount_usd": "0.04", "known": True},
    "created_at": "2026-07-18T14:30:22.104883+00:00",
    "completed_at": "2026-07-18T14:30:29.551204+00:00",
}


def test_atomic_write_bytes_creates_the_file(tmp_path):
    target = tmp_path / "nested" / "a.bin"

    atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_leaves_no_part_file(tmp_path):
    target = tmp_path / "a.bin"

    atomic_write_bytes(target, b"hello")

    assert list(tmp_path.glob("*.part")) == []


def test_atomic_write_bytes_sets_the_requested_mode(tmp_path):
    target = tmp_path / "a.bin"

    atomic_write_bytes(target, b"hello", mode=0o600)

    assert os.stat(target).st_mode & 0o777 == 0o600


def test_atomic_write_text_round_trips(tmp_path):
    target = tmp_path / "a.txt"

    atomic_write_text(target, "héllo")

    assert target.read_text(encoding="utf-8") == "héllo"


def test_part_file_promotes_on_clean_exit(tmp_path):
    target = tmp_path / "video.mp4"

    with part_file(target) as handle:
        handle.write(b"\x00\x00\x00 ftypmp42")
        assert not target.exists()

    assert target.read_bytes().startswith(b"\x00\x00\x00 ftyp")
    assert not target.with_name(target.name + ".part").exists()


def test_part_file_discards_on_exception(tmp_path):
    # Spec section 10: an interrupted download is discarded and never renamed
    # into place, so a half-file can never be indexed as a complete asset.
    target = tmp_path / "video.mp4"

    with pytest.raises(RuntimeError):
        with part_file(target) as handle:
            handle.write(b"half")
            raise RuntimeError("connection reset")

    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


def test_discard_part_is_idempotent(tmp_path):
    target = tmp_path / "video.mp4"
    target.with_name("video.mp4.part").write_bytes(b"stale")

    discard_part(target)
    discard_part(target)

    assert not target.with_name("video.mp4.part").exists()


def test_write_and_read_sidecar_round_trip(tmp_path):
    sidecar = tmp_path / "x.json"

    write_sidecar(sidecar, SIDECAR)

    assert read_sidecar(sidecar) == SIDECAR


def test_sidecar_is_sorted_and_indented(tmp_path):
    sidecar = tmp_path / "x.json"

    write_sidecar(sidecar, SIDECAR)
    text = sidecar.read_text(encoding="utf-8")

    assert text.splitlines()[1].startswith('  "')
    assert list(json.loads(text)) == sorted(SIDECAR)


def test_read_sidecar_raises_on_missing_file(tmp_path):
    with pytest.raises(SidecarError):
        read_sidecar(tmp_path / "absent.json")


def test_read_sidecar_raises_on_invalid_json(tmp_path):
    sidecar = tmp_path / "x.json"
    sidecar.write_text("{not json", encoding="utf-8")

    with pytest.raises(SidecarError):
        read_sidecar(sidecar)


def test_read_sidecar_raises_on_a_json_array(tmp_path):
    sidecar = tmp_path / "x.json"
    sidecar.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(SidecarError):
        read_sidecar(sidecar)


def test_iter_sidecars_yields_only_project_json_in_sorted_order(tmp_path):
    images = tmp_path / "projects" / "demo" / "images"
    images.mkdir(parents=True)
    (images / "b.json").write_text("{}", encoding="utf-8")
    (images / "a.json").write_text("{}", encoding="utf-8")
    (images / "a.png").write_bytes(b"x")
    thumbs = tmp_path / "thumbs" / "demo"
    thumbs.mkdir(parents=True)
    (thumbs / "ignored.json").write_text("{}", encoding="utf-8")

    found = [p.name for p in iter_sidecars(tmp_path)]

    assert found == ["a.json", "b.json"]


def test_delete_quietly_reports_absence(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")

    assert delete_quietly(target) is True
    assert delete_quietly(target) is False


def test_file_size(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"12345")

    assert file_size(target) == 5


def test_sha256_of_matches_hashlib(tmp_path):
    target = tmp_path / "a.bin"
    payload = b"x" * 300_000
    target.write_bytes(payload)

    assert sha256_of(target) == hashlib.sha256(payload).hexdigest()


def test_sidecar_cost_is_a_string_not_a_float(tmp_path):
    # Spec section 3.4: a JSON number would round-trip through a float and
    # corrupt the spend record. The contract requires a string or null.
    sidecar = tmp_path / "x.json"

    write_sidecar(sidecar, SIDECAR)
    raw = json.loads(sidecar.read_text(encoding="utf-8"))

    assert isinstance(raw["cost"]["amount_usd"], str)
    assert raw["cost"]["known"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_files.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.store.files'`

- [ ] **Step 3: Implement**

Create `src/higgshole/store/files.py`:

```python
"""Durable file writes and sidecar JSON.

Every byte written to the media tree goes through this module. The single
rule it enforces is that a partially written file is never visible under its
final name: writes land in ``<path>.part`` and are promoted with
``os.replace``, which is atomic within a filesystem. An interrupted write
therefore leaves only a ``.part`` file, which nothing indexes (spec 10).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

SIDECAR_VERSION: int = 1

_HASH_CHUNK = 1024 * 1024


class SidecarError(ValueError):
    """A sidecar was missing, unreadable, or not a JSON object."""


def _part_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry so the rename survives a power loss.

    Renaming is atomic, but the *containing directory* still needs an fsync
    for the new name to be durable. Not every platform permits opening a
    directory, so failure here is tolerated rather than fatal.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write to <path>.part, fsync, then os.replace into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path_for(path)

    with open(part, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())

    os.chmod(part, mode)
    os.replace(part, path)
    _fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


@contextmanager
def part_file(path: Path) -> Iterator[BinaryIO]:
    """Open <path>.part for streaming writes.

    On clean exit the handle is fsynced and renamed to `path`. On exception
    the .part file is unlinked and never promoted, so a failed download can
    never be mistaken for a complete asset.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path_for(path)

    handle = open(part, "wb")
    try:
        yield handle
    except BaseException:
        handle.close()
        discard_part(path)
        raise
    else:
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(part, path)
        _fsync_dir(path.parent)


def discard_part(path: Path) -> None:
    """Remove a stale <path>.part, ignoring absence."""
    _part_path_for(Path(path)).unlink(missing_ok=True)


def write_sidecar(sidecar_path: Path, payload: dict[str, Any]) -> None:
    """Serialise the sidecar as UTF-8 JSON, sorted keys, two-space indent.

    Sorted keys make the file diffable and make a rescan's ordering
    deterministic; the write is atomic because the sidecar is the
    authoritative record from which the database can be rebuilt (spec 5.3).
    """
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    atomic_write_text(Path(sidecar_path), text + "\n")


def read_sidecar(sidecar_path: Path) -> dict[str, Any]:
    """Load a sidecar. Raises SidecarError on missing file or invalid JSON."""
    path = Path(sidecar_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SidecarError(f"cannot read sidecar {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SidecarError(f"sidecar {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SidecarError(f"sidecar {path} is not a JSON object")
    return payload


def iter_sidecars(root: Path) -> Iterator[Path]:
    """Yield every '*.json' under root/projects/, in sorted order.

    Restricted to projects/ so that a stray JSON file under thumbs/ or at the
    root cannot be mistaken for a generation record during a rescan.
    """
    projects = Path(root) / "projects"
    if not projects.is_dir():
        return
    yield from sorted(projects.rglob("*.json"))


def delete_quietly(path: Path) -> bool:
    """Unlink a file, returning False if it was already absent.

    Deletion must never fail a request just because the file was gone: the
    caller's intent is that it should not exist afterwards, and it does not.
    """
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def file_size(path: Path) -> int:
    return Path(path).stat().st_size


def sha256_of(path: Path) -> str:
    """Streaming digest, used to deduplicate uploads."""
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_files.py -v`

Expected: PASS — `17 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/files.py tests/store/test_files.py
git commit -m "feat: add atomic file writes and sidecar JSON persistence"
```

---

## Task 3: Schema, shared enums, projects and settings

**Files:**
- Create: `src/higgshole/store/db.py`
- Test: `tests/store/test_db_schema.py`

**Interfaces:**
- Consumes: `store.paths.project_slug`, `store.paths.new_id`, `higgshole.config.Settings`.
- Produces: `SCHEMA_VERSION: int`, `SCHEMA_SQL: str`, `utc_now_iso() -> str`, `GenerationKind`, `GenerationState`, `IMAGE_STATES`, `VIDEO_STATES`, `TERMINAL_STATES`, `RESUMABLE_STATES`, `ErrorReason`, `AssetKind`, `InputRole`, `LedgerKind`, the row dataclasses `ProjectRow`/`GenerationRow`/`AssetRow`/`GenerationInputRow`/`LedgerRow`/`CatalogRow`/`PricingRow`, `MediaFilter`, `ID_COLLISION_RETRIES: int`, `DuplicateSlugError`, `ProjectNotEmptyError`, `IdCollisionError`, and `Database` with `from_settings`, `in_memory`, `migrate`, `close`, `__enter__`/`__exit__`, `transaction`, `create_project`, `get_project`, `get_project_by_slug`, `list_projects`, `ensure_default_project`, `delete_project`, `get_setting`, `set_setting`, `delete_setting`, `all_settings`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_db_schema.py`:

```python
import os
import sqlite3

import pytest

from higgshole.config import Settings
from higgshole.store.db import (
    IMAGE_STATES,
    RESUMABLE_STATES,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    VIDEO_STATES,
    Database,
    DuplicateSlugError,
    GenerationKind,
    GenerationState,
    ProjectNotEmptyError,
    utc_now_iso,
)

EXPECTED_TABLES = {
    "projects",
    "generations",
    "assets",
    "generation_inputs",
    "spend_ledger",
    "model_catalog",
    "model_pricing",
    "settings",
}


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


def test_migrate_creates_all_eight_tables(db):
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert EXPECTED_TABLES <= {row[0] for row in rows}


def test_migrate_is_idempotent(db):
    db.migrate()
    db.migrate()

    assert db.get_setting("schema_version") == str(SCHEMA_VERSION)


def test_migrate_records_the_schema_version(db):
    assert db.get_setting("schema_version") == str(SCHEMA_VERSION)


def test_migrate_creates_the_unsorted_project(db):
    # Spec section 5.1: agent generation must never fail for want of a project.
    project = db.get_project_by_slug("unsorted")

    assert project is not None
    assert project.name == "Unsorted"


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO generations (id, project_id, kind, model, prompt,"
                " params, state, created_at, updated_at)"
                " VALUES ('x', 'no-such-project', 'image', 'a/b', 'p', '{}',"
                " 'PENDING', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')"
            )


def test_utc_now_iso_is_utc_with_offset():
    value = utc_now_iso()

    assert value.endswith("+00:00")
    assert value[4] == "-" and value[10] == "T"


def test_state_sets_partition_by_kind():
    assert GenerationState.GENERATING in IMAGE_STATES
    assert GenerationState.GENERATING not in VIDEO_STATES
    assert GenerationState.RUNNING in VIDEO_STATES
    assert GenerationState.RUNNING not in IMAGE_STATES


def test_terminal_and_resumable_state_sets():
    assert TERMINAL_STATES == {
        GenerationState.COMPLETE,
        GenerationState.FAILED,
        GenerationState.REJECTED,
    }
    assert RESUMABLE_STATES == {GenerationState.SUBMITTED, GenerationState.RUNNING}
    assert not RESUMABLE_STATES & TERMINAL_STATES


def test_create_project_slugifies_the_name(db):
    project = db.create_project(name="My Great Project!")

    assert project.slug == "my-great-project"
    assert project.name == "My Great Project!"


def test_duplicate_slug_is_rejected(db):
    db.create_project(name="Demo")

    with pytest.raises(DuplicateSlugError):
        db.create_project(name="demo")


def test_get_project_by_slug(db):
    created = db.create_project(name="Demo")

    assert db.get_project_by_slug("demo") == created
    assert db.get_project(created.id) == created
    assert db.get_project_by_slug("absent") is None


def test_list_projects_puts_unsorted_first(db):
    db.create_project(name="Alpha")
    db.create_project(name="Beta")

    assert [p.slug for p in db.list_projects()][0] == "unsorted"
    assert {p.slug for p in db.list_projects()} == {"unsorted", "alpha", "beta"}


def test_delete_project_refuses_when_generations_exist(db):
    project = db.create_project(name="Demo")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO generations (id, project_id, kind, model, prompt, params,"
            " state, created_at, updated_at) VALUES (?, ?, ?, 'a/b', 'p', '{}',"
            " 'PENDING', ?, ?)",
            (
                "a3f21c9d4e07",
                project.id,
                GenerationKind.IMAGE.value,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )

    with pytest.raises(ProjectNotEmptyError):
        db.delete_project(project.id)


def test_delete_project_removes_an_empty_project(db):
    project = db.create_project(name="Demo")

    db.delete_project(project.id)

    assert db.get_project(project.id) is None


def test_settings_key_value_round_trip(db):
    db.set_setting("daily_cap_usd", "12.50")
    db.set_setting("daily_cap_usd", "20.00")

    assert db.get_setting("daily_cap_usd") == "20.00"
    assert db.get_setting("absent") is None


def test_delete_setting_and_all_settings(db):
    db.set_setting("favourite_models", '["a/b"]')
    db.delete_setting("favourite_models")
    db.delete_setting("favourite_models")

    assert db.get_setting("favourite_models") is None
    assert db.all_settings()["schema_version"] == str(SCHEMA_VERSION)


def test_database_file_is_created_with_owner_only_permissions(tmp_path, monkeypatch):
    # Spec section 7: the database holds API keys.
    monkeypatch.setenv("HIGGSHOLE_DB_PATH", str(tmp_path / "state" / "higgshole.db"))
    settings = Settings()

    with Database.from_settings(settings) as database:
        database.migrate()

    assert os.stat(settings.db_path).st_mode & 0o777 == 0o600
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_db_schema.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.store.db'`

- [ ] **Step 3: Implement**

Create `src/higgshole/store/db.py`:

```python
"""SQLite schema, shared vocabulary, and every query in the application.

The database is a *partially rebuildable index* (spec 5.3): generations,
assets, projects and lineage can be reconstructed from sidecars, but the spend
ledger and stored credentials exist nowhere else.

Access is synchronous ``sqlite3`` throughout. Async callers wrap bulk calls in
``anyio.to_thread.run_sync``; the work here is sub-millisecond and a
single-worker deployment (spec 9) makes a connection pool pointless, while a
synchronous API keeps transactions trivially correct.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from higgshole.config import Settings

from .paths import new_id, project_slug

SCHEMA_VERSION: int = 1

#: Fresh identifiers to try before concluding the RNG is broken. At 48 bits,
#: five consecutive collisions is not bad luck.
ID_COLLISION_RETRIES: int = 5


# -- shared vocabulary ----------------------------------------------------


class GenerationKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class GenerationState(StrEnum):
    """Union of both state machines (spec 4.3).

    PENDING/REJECTED/FAILED/COMPLETE are shared. GENERATING and WRITING are
    image-only; SUBMITTED, RUNNING and DOWNLOADING are video-only. A single
    column carries both machines because no row is ever ambiguous: `kind`
    determines which machine applies.
    """

    PENDING = "PENDING"
    REJECTED = "REJECTED"
    # image-only
    GENERATING = "GENERATING"
    WRITING = "WRITING"
    # video-only
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    DOWNLOADING = "DOWNLOADING"
    # shared terminal
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


IMAGE_STATES: frozenset[GenerationState] = frozenset(
    {
        GenerationState.PENDING,
        GenerationState.GENERATING,
        GenerationState.WRITING,
        GenerationState.COMPLETE,
        GenerationState.REJECTED,
        GenerationState.FAILED,
    }
)

VIDEO_STATES: frozenset[GenerationState] = frozenset(
    {
        GenerationState.PENDING,
        GenerationState.SUBMITTED,
        GenerationState.RUNNING,
        GenerationState.DOWNLOADING,
        GenerationState.COMPLETE,
        GenerationState.REJECTED,
        GenerationState.FAILED,
    }
)

TERMINAL_STATES: frozenset[GenerationState] = frozenset(
    {GenerationState.COMPLETE, GenerationState.FAILED, GenerationState.REJECTED}
)

#: Video rows in these states are reattached to pollers at boot (spec 4.3).
#: Image rows can never occupy them, so no kind filter is strictly required —
#: but resume.py filters on kind anyway as a defence against corruption.
RESUMABLE_STATES: frozenset[GenerationState] = frozenset(
    {GenerationState.SUBMITTED, GenerationState.RUNNING}
)


class ErrorReason(StrEnum):
    """Machine-readable failure causes. Spec 10 defines operator behaviour."""

    VALIDATION = "validation"
    CAP_EXCEEDED = "cap_exceeded"
    IN_FLIGHT_LIMIT = "in_flight_limit"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    MODERATION = "moderation"
    INDETERMINATE = "indeterminate"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_CANCELLED = "provider_cancelled"
    PROVIDER_EXPIRED = "provider_expired"
    TIMEOUT = "timeout"
    DOWNLOAD_FAILED = "download_failed"
    WRITE_FAILED = "write_failed"
    INTERNAL = "internal"


class AssetKind(StrEnum):
    UPLOAD = "upload"
    OUTPUT = "output"
    THUMBNAIL = "thumbnail"
    POSTER = "poster"


class InputRole(StrEnum):
    INPUT_REFERENCE = "input_reference"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"


class LedgerKind(StrEnum):
    RESERVATION = "reservation"
    REVERSAL = "reversal"
    ACTUAL = "actual"


def utc_now_iso() -> str:
    """Current UTC time as 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'.

    Stored as TEXT rather than a SQLite timestamp so that the UTC-day cap
    window (spec 3.3) can be computed by string prefix comparison, which is
    exact and index-friendly under SQLite's default BINARY collation.
    """
    return datetime.now(UTC).isoformat()


# -- errors ---------------------------------------------------------------


class DuplicateSlugError(ValueError):
    """A project with this slug already exists."""


class ProjectNotEmptyError(ValueError):
    """The project still has generations; deleting it is an explicit act."""


class IdCollisionError(RuntimeError):
    """ID_COLLISION_RETRIES fresh identifiers all collided.

    At 48 bits this signals a broken RNG, not bad luck.
    """


# -- row types ------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRow:
    id: str
    slug: str
    name: str
    created_at: str


@dataclass(frozen=True)
class GenerationRow:
    id: str
    project_id: str
    kind: GenerationKind
    model: str
    prompt: str
    params: dict[str, Any]
    state: GenerationState
    provider_job_id: str | None
    file_path: str | None
    error_reason: ErrorReason | None
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AssetRow:
    id: str
    generation_id: str | None
    kind: AssetKind
    file_path: str
    mime_type: str
    bytes: int
    width: int | None
    height: int | None
    duration_s: float | None
    created_at: str


@dataclass(frozen=True)
class GenerationInputRow:
    generation_id: str
    asset_id: str
    role: InputRole
    position: int


@dataclass(frozen=True)
class LedgerRow:
    id: int
    generation_id: str
    kind: LedgerKind
    amount: Decimal
    cost_known: bool
    recorded_at: str


@dataclass(frozen=True)
class CatalogRow:
    model_id: str
    kind: GenerationKind
    capabilities: dict[str, Any]
    fetched_at: str


@dataclass(frozen=True)
class PricingRow:
    model_id: str
    pricing: list[dict[str, Any]]
    fetched_at: str


@dataclass(frozen=True)
class MediaFilter:
    """Library browse filters (spec 6.1). `None` means unfiltered."""

    project_slug: str | None = None
    kind: GenerationKind | None = None
    model: str | None = None
    state: GenerationState | None = None
    created_after: str | None = None
    created_before: str | None = None
    limit: int = 50
    offset: int = 0


# -- schema ---------------------------------------------------------------

SCHEMA_SQL: str = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind             TEXT NOT NULL CHECK (kind IN ('image','video')),
    model            TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL CHECK (state IN (
                        'PENDING','REJECTED','GENERATING','WRITING',
                        'SUBMITTED','RUNNING','DOWNLOADING','COMPLETE','FAILED')),
    provider_job_id  TEXT,
    file_path        TEXT,
    error_reason     TEXT,
    error_detail     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_generations_project_created
    ON generations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generations_state
    ON generations(state);
CREATE INDEX IF NOT EXISTS idx_generations_kind_created
    ON generations(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generations_model
    ON generations(model);
CREATE UNIQUE INDEX IF NOT EXISTS idx_generations_provider_job
    ON generations(provider_job_id) WHERE provider_job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS assets (
    id             TEXT PRIMARY KEY,
    generation_id  TEXT REFERENCES generations(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('upload','output','thumbnail','poster')),
    file_path      TEXT NOT NULL UNIQUE,
    mime_type      TEXT NOT NULL,
    bytes          INTEGER NOT NULL,
    width          INTEGER,
    height         INTEGER,
    duration_s     REAL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_generation ON assets(generation_id);
CREATE INDEX IF NOT EXISTS idx_assets_kind_created ON assets(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS generation_inputs (
    generation_id  TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    asset_id       TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
    role           TEXT NOT NULL CHECK (role IN ('input_reference','first_frame','last_frame')),
    position       INTEGER NOT NULL,
    PRIMARY KEY (generation_id, role, position)
);

CREATE INDEX IF NOT EXISTS idx_generation_inputs_asset ON generation_inputs(asset_id);

CREATE TABLE IF NOT EXISTS spend_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id  TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('reservation','reversal','actual')),
    amount         TEXT NOT NULL,
    cost_known     INTEGER NOT NULL CHECK (cost_known IN (0,1)),
    recorded_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spend_ledger_recorded ON spend_ledger(recorded_at);
CREATE INDEX IF NOT EXISTS idx_spend_ledger_generation ON spend_ledger(generation_id);

CREATE TABLE IF NOT EXISTS model_catalog (
    model_id      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
    capabilities  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (model_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_model_catalog_kind ON model_catalog(kind);

CREATE TABLE IF NOT EXISTS model_pricing (
    model_id    TEXT PRIMARY KEY,
    pricing     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


# -- the database ---------------------------------------------------------


class Database:
    def __init__(self, path: Path, *, _uri: str | None = None) -> None:
        """Open the database, creating parent directories.

        The file is chmodded 0600 because it holds API keys (spec 7). The
        private ``_uri`` argument exists solely for ``in_memory``.
        """
        if _uri is not None:
            self._path = Path(":memory:")
            self._conn = sqlite3.connect(_uri, uri=True, check_same_thread=False)
        else:
            self._path = Path(path).expanduser()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            existed = self._path.exists()
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            if not existed:
                os.chmod(self._path, 0o600)

        # Autocommit: transactions are begun explicitly so their extent is
        # visible at the call site rather than inferred from statement types.
        self._conn.isolation_level = None
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(settings.db_path)

    @classmethod
    def in_memory(cls) -> Database:
        """For tests. A uniquely named shared-cache in-memory database."""
        name = f"file:higgshole-{uuid.uuid4().hex}?mode=memory&cache=shared"
        return cls(Path(":memory:"), _uri=name)

    def migrate(self) -> None:
        """Apply SCHEMA_SQL and record SCHEMA_VERSION. Idempotent."""
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.set_setting("schema_version", str(SCHEMA_VERSION))
        self.ensure_default_project()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, rolling back on exception.

        IMMEDIATE rather than DEFERRED so that a write lock is taken up front:
        the reservation gate reads and writes inside one transaction and must
        not be able to upgrade-deadlock against itself.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- projects -------------------------------------------------------

    @staticmethod
    def _project(row: sqlite3.Row) -> ProjectRow:
        return ProjectRow(
            id=row["id"], slug=row["slug"], name=row["name"], created_at=row["created_at"]
        )

    def create_project(self, *, name: str, slug: str | None = None) -> ProjectRow:
        """Slug defaults to project_slug(name). Raises DuplicateSlugError."""
        resolved = slug or project_slug(name)
        record = ProjectRow(
            id=new_id(), slug=resolved, name=name, created_at=utc_now_iso()
        )
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO projects (id, slug, name, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (record.id, record.slug, record.name, record.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSlugError(f"project slug already exists: {resolved}") from exc
        return record

    def get_project(self, project_id: str) -> ProjectRow | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._project(row) if row else None

    def get_project_by_slug(self, slug: str) -> ProjectRow | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        return self._project(row) if row else None

    def list_projects(self) -> list[ProjectRow]:
        """Ordered by created_at ascending; 'unsorted' is always first."""
        rows = self._conn.execute(
            "SELECT * FROM projects"
            " ORDER BY (slug = 'unsorted') DESC, created_at ASC, slug ASC"
        ).fetchall()
        return [self._project(row) for row in rows]

    def ensure_default_project(self) -> ProjectRow:
        """Create the 'unsorted' project if absent (spec 5.1)."""
        existing = self.get_project_by_slug("unsorted")
        if existing is not None:
            return existing
        return self.create_project(name="Unsorted", slug="unsorted")

    def delete_project(self, project_id: str) -> None:
        """Raises ProjectNotEmptyError if generations reference it."""
        count = self._conn.execute(
            "SELECT COUNT(*) FROM generations WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        if count:
            raise ProjectNotEmptyError(
                f"project {project_id} still has {count} generation(s)"
            )
        with self.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # -- settings -------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all_settings(self) -> dict[str, str]:
        """Never returned to a client unmasked — see web/api.py key masking."""
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}
```

> `json`, `field`, `Iterable`, `Sequence` and `AssetRow`-adjacent helpers are imported here because Tasks 4 and 5 append methods to this same class without revisiting the import block. `ruff` will flag them as unused until Task 4 lands; run `uv run ruff check .` only at the end of Task 5.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_db_schema.py -v`

Expected: PASS — `17 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/db.py tests/store/test_db_schema.py
git commit -m "feat: add SQLite schema, shared enums, projects and settings"
```

---

## Task 4: Generations, assets and lineage

**Files:**
- Modify: `src/higgshole/store/db.py` (append methods to `Database`)
- Test: `tests/store/test_db_generations.py`

**Interfaces:**
- Consumes: `GenerationKind`, `GenerationState`, `ErrorReason`, `AssetKind`, `InputRole`, `MediaFilter`, `TERMINAL_STATES`, `utc_now_iso`, `new_id`, `ID_COLLISION_RETRIES`, `IdCollisionError` (all Task 3).
- Produces on `Database`: `create_generation`, `get_generation`, `set_generation_state`, `set_provider_job_id`, `set_generation_file`, `list_generations`, `count_generations`, `list_generations_in_states`, `count_in_flight`, `delete_generation`, `create_asset`, `get_asset`, `get_asset_by_path`, `list_assets_for_generation`, `list_uploads`, `delete_asset`, `add_generation_input`, `list_generation_inputs`, `list_generation_children`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_db_generations.py`:

```python
import sqlite3

import pytest

from higgshole.store.db import (
    AssetKind,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationState,
    IdCollisionError,
    InputRole,
    MediaFilter,
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def project(db):
    return db.get_project_by_slug("unsorted")


def make_generation(db, project, **overrides):
    kwargs = {
        "project_id": project.id,
        "kind": GenerationKind.IMAGE,
        "model": "openai/gpt-image-2",
        "prompt": "a cat",
        "params": {},
    }
    kwargs.update(overrides)
    return db.create_generation(**kwargs)


def test_create_generation_starts_pending(db, project):
    row = make_generation(db, project)

    assert row.state is GenerationState.PENDING
    assert row.kind is GenerationKind.IMAGE
    assert len(row.id) == 12
    assert row.completed_at is None
    assert db.get_generation(row.id) == row


def test_create_generation_round_trips_params_json(db, project):
    row = make_generation(db, project, params={"aspect_ratio": "16:9", "seed": 7})

    assert db.get_generation(row.id).params == {"aspect_ratio": "16:9", "seed": 7}


def test_create_generation_retries_on_id_collision(db, project, monkeypatch):
    taken = make_generation(db, project).id
    ids = iter([taken, taken, "b7e004aa1c32"])
    monkeypatch.setattr("higgshole.store.db.new_id", lambda: next(ids))

    row = make_generation(db, project)

    assert row.id == "b7e004aa1c32"


def test_id_collision_exhaustion_raises(db, project, monkeypatch):
    taken = make_generation(db, project).id
    monkeypatch.setattr("higgshole.store.db.new_id", lambda: taken)

    with pytest.raises(IdCollisionError):
        make_generation(db, project)


def test_set_generation_state_stamps_updated_at(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(row.id, GenerationState.GENERATING)

    assert updated.state is GenerationState.GENERATING
    assert updated.updated_at >= row.updated_at
    assert updated.completed_at is None


def test_terminal_state_stamps_completed_at(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(row.id, GenerationState.COMPLETE)

    assert updated.completed_at is not None


def test_set_generation_state_records_the_error_reason(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(
        row.id,
        GenerationState.FAILED,
        error_reason=ErrorReason.INDETERMINATE,
        error_detail="connection reset after submit",
    )

    assert updated.error_reason is ErrorReason.INDETERMINATE
    assert updated.error_detail == "connection reset after submit"


def test_set_provider_job_id(db, project):
    row = make_generation(db, project, kind=GenerationKind.VIDEO)

    db.set_provider_job_id(row.id, "job-abc")

    assert db.get_generation(row.id).provider_job_id == "job-abc"


def test_duplicate_provider_job_id_is_rejected(db, project):
    # Spec section 9: double reattachment must be a database error, not a
    # duplicate download.
    first = make_generation(db, project, kind=GenerationKind.VIDEO)
    second = make_generation(db, project, kind=GenerationKind.VIDEO)
    db.set_provider_job_id(first.id, "job-abc")

    with pytest.raises(sqlite3.IntegrityError):
        db.set_provider_job_id(second.id, "job-abc")


def test_multiple_null_provider_job_ids_are_allowed(db, project):
    make_generation(db, project)
    make_generation(db, project)

    assert db.count_generations(MediaFilter()) == 2


def test_set_generation_file(db, project):
    row = make_generation(db, project)

    db.set_generation_file(row.id, "projects/unsorted/images/x.png")

    assert db.get_generation(row.id).file_path == "projects/unsorted/images/x.png"


def test_list_generations_newest_first(db, project):
    first = make_generation(db, project, prompt="one")
    second = make_generation(db, project, prompt="two")

    listed = db.list_generations(MediaFilter())

    assert [g.id for g in listed] == [second.id, first.id]


def test_list_generations_filters_by_kind(db, project):
    make_generation(db, project)
    video = make_generation(db, project, kind=GenerationKind.VIDEO)

    listed = db.list_generations(MediaFilter(kind=GenerationKind.VIDEO))

    assert [g.id for g in listed] == [video.id]


def test_list_generations_filters_by_project_slug(db, project):
    other = db.create_project(name="Other")
    make_generation(db, project)
    elsewhere = make_generation(db, other)

    listed = db.list_generations(MediaFilter(project_slug="other"))

    assert [g.id for g in listed] == [elsewhere.id]


def test_list_generations_filters_by_date_window(db, project):
    row = make_generation(db, project)
    created = db.get_generation(row.id).created_at

    assert db.list_generations(MediaFilter(created_after=created)) != []
    assert db.list_generations(MediaFilter(created_before=created)) == []


def test_count_generations_ignores_pagination(db, project):
    for _ in range(5):
        make_generation(db, project)

    listed = db.list_generations(MediaFilter(limit=2, offset=1))

    assert len(listed) == 2
    assert db.count_generations(MediaFilter(limit=2, offset=1)) == 5


def test_list_generations_in_states(db, project):
    pending = make_generation(db, project, kind=GenerationKind.VIDEO)
    running = make_generation(db, project, kind=GenerationKind.VIDEO)
    db.set_generation_state(running.id, GenerationState.RUNNING)
    make_generation(db, project)

    found = db.list_generations_in_states(
        [GenerationState.RUNNING], kind=GenerationKind.VIDEO
    )

    assert [g.id for g in found] == [running.id]
    assert pending.id not in {g.id for g in found}


def test_count_in_flight_excludes_terminal_states(db, project):
    make_generation(db, project)
    done = make_generation(db, project)
    db.set_generation_state(done.id, GenerationState.COMPLETE)
    rejected = make_generation(db, project)
    db.set_generation_state(rejected.id, GenerationState.REJECTED)

    assert db.count_in_flight() == 1


def test_create_and_get_asset(db, project):
    generation = make_generation(db, project)

    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="projects/unsorted/images/x.png",
        mime_type="image/png",
        bytes_=1024,
        generation_id=generation.id,
        width=1920,
        height=1080,
    )

    assert db.get_asset(asset.id) == asset
    assert asset.duration_s is None


def test_get_asset_by_path(db):
    asset = db.create_asset(
        kind=AssetKind.UPLOAD,
        file_path="projects/unsorted/uploads/ref.png",
        mime_type="image/png",
        bytes_=10,
    )

    assert db.get_asset_by_path("projects/unsorted/uploads/ref.png") == asset
    assert db.get_asset_by_path("nope") is None


def test_list_assets_for_generation(db, project):
    generation = make_generation(db, project)
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(
        kind=AssetKind.THUMBNAIL,
        file_path="t.webp",
        mime_type="image/webp",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1)

    assert {a.kind for a in db.list_assets_for_generation(generation.id)} == {
        AssetKind.OUTPUT,
        AssetKind.THUMBNAIL,
    }


def test_list_uploads(db, project):
    generation = make_generation(db, project)
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    upload = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1
    )

    assert [a.id for a in db.list_uploads()] == [upload.id]


def test_delete_asset_returns_its_path(db):
    asset = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1
    )

    assert db.delete_asset(asset.id) == "u.png"
    assert db.delete_asset(asset.id) is None


def test_delete_generation_returns_every_file_path(db, project):
    generation = make_generation(db, project)
    db.set_generation_file(generation.id, "projects/unsorted/images/x.png")
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="projects/unsorted/images/x.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(
        kind=AssetKind.THUMBNAIL,
        file_path="thumbs/unsorted/x.webp",
        mime_type="image/webp",
        bytes_=1,
        generation_id=generation.id,
    )

    paths = db.delete_generation(generation.id)

    assert set(paths) == {
        "projects/unsorted/images/x.png",
        "thumbs/unsorted/x.webp",
    }


def test_delete_generation_cascades_assets(db, project):
    generation = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )

    db.delete_generation(generation.id)

    assert db.get_generation(generation.id) is None
    assert db.get_asset(asset.id) is None


def test_delete_generation_is_refused_when_an_asset_feeds_another_generation(db, project):
    # generation_inputs.asset_id is ON DELETE RESTRICT: lineage is not silently
    # discarded (spec section 4.5).
    parent = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=parent.id,
    )
    child = make_generation(db, project)
    db.add_generation_input(
        generation_id=child.id,
        asset_id=asset.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.delete_generation(parent.id)


def test_add_and_list_generation_inputs(db, project):
    generation = make_generation(db, project)
    first = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u1.png", mime_type="image/png", bytes_=1
    )
    second = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u2.png", mime_type="image/png", bytes_=1
    )
    db.add_generation_input(
        generation_id=generation.id,
        asset_id=second.id,
        role=InputRole.INPUT_REFERENCE,
        position=1,
    )
    db.add_generation_input(
        generation_id=generation.id,
        asset_id=first.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    inputs = db.list_generation_inputs(generation.id)

    assert [i.asset_id for i in inputs] == [first.id, second.id]
    assert inputs[0].role is InputRole.INPUT_REFERENCE


def test_list_generation_children(db, project):
    parent = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=parent.id,
    )
    child = make_generation(db, project, prompt="make it watercolour")
    db.add_generation_input(
        generation_id=child.id,
        asset_id=asset.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    assert [g.id for g in db.list_generation_children(asset.id)] == [child.id]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_db_generations.py -v`

Expected: FAIL — `AttributeError: 'Database' object has no attribute 'create_generation'`

- [ ] **Step 3: Implement**

Append these methods to the `Database` class in `src/higgshole/store/db.py`, after `all_settings`:

```python
    # -- generations ----------------------------------------------------

    @staticmethod
    def _generation(row: sqlite3.Row) -> GenerationRow:
        return GenerationRow(
            id=row["id"],
            project_id=row["project_id"],
            kind=GenerationKind(row["kind"]),
            model=row["model"],
            prompt=row["prompt"],
            params=json.loads(row["params"]),
            state=GenerationState(row["state"]),
            provider_job_id=row["provider_job_id"],
            file_path=row["file_path"],
            error_reason=(
                ErrorReason(row["error_reason"]) if row["error_reason"] else None
            ),
            error_detail=row["error_detail"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def create_generation(
        self,
        *,
        project_id: str,
        kind: GenerationKind,
        model: str,
        prompt: str,
        params: dict[str, Any],
        state: GenerationState = GenerationState.PENDING,
        gen_id: str | None = None,
    ) -> GenerationRow:
        """Insert a generation, retrying on a UNIQUE violation.

        Retrying rather than pre-checking is deliberate: a SELECT-then-INSERT
        would still race, whereas the UNIQUE constraint is authoritative and
        the retry is free at 48 bits (spec 5.1).
        """
        now = utc_now_iso()
        attempts = 1 if gen_id else ID_COLLISION_RETRIES

        for _ in range(attempts):
            candidate = gen_id or new_id()
            try:
                with self.transaction() as conn:
                    conn.execute(
                        "INSERT INTO generations (id, project_id, kind, model,"
                        " prompt, params, state, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            candidate,
                            project_id,
                            GenerationKind(kind).value,
                            model,
                            prompt,
                            json.dumps(params, sort_keys=True),
                            GenerationState(state).value,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError:
                if gen_id is not None:
                    raise
                continue
            return GenerationRow(
                id=candidate,
                project_id=project_id,
                kind=GenerationKind(kind),
                model=model,
                prompt=prompt,
                params=dict(params),
                state=GenerationState(state),
                provider_job_id=None,
                file_path=None,
                error_reason=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )

        raise IdCollisionError(
            f"{ID_COLLISION_RETRIES} fresh identifiers all collided"
        )

    def get_generation(self, gen_id: str) -> GenerationRow | None:
        row = self._conn.execute(
            "SELECT * FROM generations WHERE id = ?", (gen_id,)
        ).fetchone()
        return self._generation(row) if row else None

    def set_generation_state(
        self,
        gen_id: str,
        state: GenerationState,
        *,
        error_reason: ErrorReason | None = None,
        error_detail: str | None = None,
        completed_at: str | None = None,
    ) -> GenerationRow:
        """Update state and updated_at atomically.

        A terminal state with no explicit completed_at stamps one, so no code
        path can leave a finished generation without a completion time.
        """
        now = utc_now_iso()
        finished = completed_at
        if finished is None and GenerationState(state) in TERMINAL_STATES:
            finished = now

        with self.transaction() as conn:
            conn.execute(
                "UPDATE generations SET state = ?, updated_at = ?,"
                " error_reason = COALESCE(?, error_reason),"
                " error_detail = COALESCE(?, error_detail),"
                " completed_at = COALESCE(?, completed_at)"
                " WHERE id = ?",
                (
                    GenerationState(state).value,
                    now,
                    error_reason.value if error_reason else None,
                    error_detail,
                    finished,
                    gen_id,
                ),
            )

        updated = self.get_generation(gen_id)
        if updated is None:
            raise KeyError(f"no such generation: {gen_id}")
        return updated

    def set_provider_job_id(self, gen_id: str, provider_job_id: str) -> None:
        """Commit the provider job ID BEFORE polling begins (spec 4.3).

        A separate call precisely so the ordering is visible at the call site.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE generations SET provider_job_id = ?, updated_at = ?"
                " WHERE id = ?",
                (provider_job_id, utc_now_iso(), gen_id),
            )

    def set_generation_file(self, gen_id: str, relative_path: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE generations SET file_path = ?, updated_at = ? WHERE id = ?",
                (relative_path, utc_now_iso(), gen_id),
            )

    @staticmethod
    def _filter_sql(filters: MediaFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if filters.project_slug is not None:
            clauses.append(
                "g.project_id IN (SELECT id FROM projects WHERE slug = ?)"
            )
            params.append(filters.project_slug)
        if filters.kind is not None:
            clauses.append("g.kind = ?")
            params.append(GenerationKind(filters.kind).value)
        if filters.model is not None:
            clauses.append("g.model = ?")
            params.append(filters.model)
        if filters.state is not None:
            clauses.append("g.state = ?")
            params.append(GenerationState(filters.state).value)
        if filters.created_after is not None:
            clauses.append("g.created_at >= ?")
            params.append(filters.created_after)
        if filters.created_before is not None:
            clauses.append("g.created_at < ?")
            params.append(filters.created_before)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_generations(self, filters: MediaFilter) -> list[GenerationRow]:
        """Newest first. Applies every non-None field of `filters`."""
        where, params = self._filter_sql(filters)
        rows = self._conn.execute(
            f"SELECT g.* FROM generations g{where}"
            " ORDER BY g.created_at DESC, g.rowid DESC LIMIT ? OFFSET ?",
            [*params, filters.limit, filters.offset],
        ).fetchall()
        return [self._generation(row) for row in rows]

    def count_generations(self, filters: MediaFilter) -> int:
        """Total matching rows, ignoring limit/offset — for pagination."""
        where, params = self._filter_sql(filters)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM generations g{where}", params
        ).fetchone()[0]

    def list_generations_in_states(
        self,
        states: Iterable[GenerationState],
        *,
        kind: GenerationKind | None = None,
    ) -> list[GenerationRow]:
        """Backs boot-time reattachment and the in-flight count."""
        wanted = [GenerationState(s).value for s in states]
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        sql = f"SELECT * FROM generations WHERE state IN ({placeholders})"
        params: list[Any] = list(wanted)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(GenerationKind(kind).value)
        rows = self._conn.execute(sql + " ORDER BY created_at ASC", params).fetchall()
        return [self._generation(row) for row in rows]

    def count_in_flight(self) -> int:
        """Generations in any non-terminal state.

        Read inside the budget gate's lock (spec 3.3).
        """
        terminal = [state.value for state in sorted(TERMINAL_STATES)]
        placeholders = ", ".join("?" for _ in terminal)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM generations WHERE state NOT IN ({placeholders})",
            terminal,
        ).fetchone()[0]

    def delete_generation(self, gen_id: str) -> list[str]:
        """Delete the row and its assets, returning the paths to unlink.

        The database never touches the disk: it reports what should be removed
        and the caller does the removing, so a filesystem error cannot leave a
        half-committed transaction.
        """
        generation = self.get_generation(gen_id)
        if generation is None:
            return []

        paths = {asset.file_path for asset in self.list_assets_for_generation(gen_id)}
        if generation.file_path:
            paths.add(generation.file_path)

        with self.transaction() as conn:
            conn.execute("DELETE FROM generations WHERE id = ?", (gen_id,))

        return sorted(paths)

    # -- assets ---------------------------------------------------------

    @staticmethod
    def _asset(row: sqlite3.Row) -> AssetRow:
        return AssetRow(
            id=row["id"],
            generation_id=row["generation_id"],
            kind=AssetKind(row["kind"]),
            file_path=row["file_path"],
            mime_type=row["mime_type"],
            bytes=row["bytes"],
            width=row["width"],
            height=row["height"],
            duration_s=row["duration_s"],
            created_at=row["created_at"],
        )

    def create_asset(
        self,
        *,
        kind: AssetKind,
        file_path: str,
        mime_type: str,
        bytes_: int,
        generation_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_s: float | None = None,
        asset_id: str | None = None,
    ) -> AssetRow:
        record = AssetRow(
            id=asset_id or new_id(),
            generation_id=generation_id,
            kind=AssetKind(kind),
            file_path=file_path,
            mime_type=mime_type,
            bytes=bytes_,
            width=width,
            height=height,
            duration_s=duration_s,
            created_at=utc_now_iso(),
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO assets (id, generation_id, kind, file_path, mime_type,"
                " bytes, width, height, duration_s, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.generation_id,
                    record.kind.value,
                    record.file_path,
                    record.mime_type,
                    record.bytes,
                    record.width,
                    record.height,
                    record.duration_s,
                    record.created_at,
                ),
            )
        return record

    def get_asset(self, asset_id: str) -> AssetRow | None:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        return self._asset(row) if row else None

    def get_asset_by_path(self, file_path: str) -> AssetRow | None:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE file_path = ?", (file_path,)
        ).fetchone()
        return self._asset(row) if row else None

    def list_assets_for_generation(self, gen_id: str) -> list[AssetRow]:
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE generation_id = ? ORDER BY kind, created_at",
            (gen_id,),
        ).fetchall()
        return [self._asset(row) for row in rows]

    def list_uploads(
        self, *, project_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[AssetRow]:
        """Uploads are identified by kind, not by a NULL generation_id.

        Project scoping uses the stored relative path, because an upload has no
        foreign key to a project — its project is expressed by where it lives.
        """
        sql = "SELECT * FROM assets WHERE kind = ?"
        params: list[Any] = [AssetKind.UPLOAD.value]
        if project_id is not None:
            project = self.get_project(project_id)
            if project is None:
                return []
            sql += " AND file_path LIKE ?"
            params.append(f"projects/{project.slug}/uploads/%")
        rows = self._conn.execute(
            sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [self._asset(row) for row in rows]

    def delete_asset(self, asset_id: str) -> str | None:
        """Delete the row, returning its relative path, or None if absent."""
        asset = self.get_asset(asset_id)
        if asset is None:
            return None
        with self.transaction() as conn:
            conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return asset.file_path

    # -- lineage --------------------------------------------------------

    def add_generation_input(
        self, *, generation_id: str, asset_id: str, role: InputRole, position: int
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO generation_inputs (generation_id, asset_id, role,"
                " position) VALUES (?, ?, ?, ?)",
                (generation_id, asset_id, InputRole(role).value, position),
            )

    def list_generation_inputs(self, gen_id: str) -> list[GenerationInputRow]:
        """Ordered by role then position."""
        rows = self._conn.execute(
            "SELECT * FROM generation_inputs WHERE generation_id = ?"
            " ORDER BY role, position",
            (gen_id,),
        ).fetchall()
        return [
            GenerationInputRow(
                generation_id=row["generation_id"],
                asset_id=row["asset_id"],
                role=InputRole(row["role"]),
                position=row["position"],
            )
            for row in rows
        ]

    def list_generation_children(self, asset_id: str) -> list[GenerationRow]:
        """Generations that used this asset as an input (spec 4.5)."""
        rows = self._conn.execute(
            "SELECT g.* FROM generations g"
            " JOIN generation_inputs i ON i.generation_id = g.id"
            " WHERE i.asset_id = ? ORDER BY g.created_at DESC",
            (asset_id,),
        ).fetchall()
        return [self._generation(row) for row in rows]
```

> `list_generations` orders by `created_at DESC, rowid DESC` because two generations created inside the same microsecond would otherwise have an unstable order, and the newest-first test would flake.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_db_generations.py -v`

Expected: PASS — `28 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/db.py tests/store/test_db_generations.py
git commit -m "feat: add generation, asset and lineage queries"
```

---

## Task 5: Ledger rows, catalogue and pricing storage

**Files:**
- Modify: `src/higgshole/store/db.py` (append methods to `Database`)
- Test: `tests/store/test_db_catalog_ledger.py`

**Interfaces:**
- Consumes: `LedgerKind`, `LedgerRow`, `CatalogRow`, `PricingRow`, `GenerationKind`, `utc_now_iso` (Task 3).
- Produces on `Database`: `append_ledger`, `list_ledger_for_generation`, `list_ledger_between`, `upsert_catalog`, `replace_catalog`, `list_catalog`, `get_catalog`, `catalog_fetched_at`, `upsert_pricing`, `get_pricing`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_db_catalog_ledger.py`:

```python
from decimal import Decimal

import pytest

from higgshole.store.db import (
    Database,
    GenerationKind,
    GenerationState,
    LedgerKind,
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def generation(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    )


def test_append_ledger_stores_amount_as_text(db, generation):
    # SQLite REAL would round money; the contract mandates TEXT.
    row = db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.RESERVATION,
        amount=Decimal("2.00"),
        cost_known=False,
    )

    assert row.amount == Decimal("2.00")
    assert row.kind is LedgerKind.RESERVATION
    assert row.cost_known is False
    stored = db._conn.execute("SELECT amount FROM spend_ledger").fetchone()[0]
    assert isinstance(stored, str)
    assert stored == "2.00"


def test_negative_amounts_round_trip(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.REVERSAL,
        amount=Decimal("-2.00"),
        cost_known=False,
    )

    assert db.list_ledger_for_generation(generation.id)[0].amount == Decimal("-2.00")


def test_list_ledger_for_generation_in_order(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.RESERVATION,
        amount=Decimal("2.00"),
        cost_known=False,
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.REVERSAL,
        amount=Decimal("-2.00"),
        cost_known=False,
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("0.25"),
        cost_known=True,
    )

    kinds = [row.kind for row in db.list_ledger_for_generation(generation.id)]

    assert kinds == [LedgerKind.RESERVATION, LedgerKind.REVERSAL, LedgerKind.ACTUAL]


def test_list_ledger_between_is_half_open(db, generation, monkeypatch):
    monkeypatch.setattr(
        "higgshole.store.db.utc_now_iso", lambda: "2026-07-18T00:00:00+00:00"
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("1.00"),
        cost_known=True,
    )
    monkeypatch.setattr(
        "higgshole.store.db.utc_now_iso", lambda: "2026-07-19T00:00:00+00:00"
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("2.00"),
        cost_known=True,
    )

    rows = db.list_ledger_between(
        start_iso="2026-07-18T00:00:00+00:00", end_iso="2026-07-19T00:00:00+00:00"
    )

    assert [row.amount for row in rows] == [Decimal("1.00")]


def test_ledger_cascades_when_a_generation_is_deleted(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("1.00"),
        cost_known=True,
    )
    db.set_generation_state(generation.id, GenerationState.COMPLETE)

    db.delete_generation(generation.id)

    assert db.list_ledger_for_generation(generation.id) == []


def test_upsert_catalog_round_trips_capabilities(db):
    db.upsert_catalog(
        model_id="google/veo-3.1",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "google/veo-3.1", "supported_durations": [4, 6, 8]},
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    row = db.get_catalog("google/veo-3.1", GenerationKind.VIDEO)

    assert row is not None
    assert row.capabilities["supported_durations"] == [4, 6, 8]
    assert row.kind is GenerationKind.VIDEO


def test_replace_catalog_replaces_the_whole_kind(db):
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("a/one", {"id": "a/one"}), ("a/two", {"id": "a/two"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("a/three", {"id": "a/three"})],
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    assert [r.model_id for r in db.list_catalog(GenerationKind.VIDEO)] == ["a/three"]


def test_replace_catalog_leaves_the_other_kind_alone(db):
    db.replace_catalog(
        GenerationKind.IMAGE,
        [("i/one", {"id": "i/one"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("v/one", {"id": "v/one"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    assert [r.model_id for r in db.list_catalog(GenerationKind.IMAGE)] == ["i/one"]
    assert len(db.list_catalog()) == 2


def test_catalog_fetched_at_returns_the_oldest(db):
    db.upsert_catalog(
        model_id="a/one",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/one"},
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.upsert_catalog(
        model_id="a/two",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/two"},
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    assert db.catalog_fetched_at(GenerationKind.VIDEO) == "2026-07-18T00:00:00+00:00"


def test_catalog_fetched_at_is_none_when_empty(db):
    assert db.catalog_fetched_at(GenerationKind.IMAGE) is None


def test_get_catalog_by_id_and_kind(db):
    db.upsert_catalog(
        model_id="a/one",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/one"},
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    assert db.get_catalog("a/one", GenerationKind.IMAGE) is None
    assert db.get_catalog("absent", GenerationKind.VIDEO) is None


def test_upsert_pricing_round_trips_a_line_item_array(db):
    items = [
        {"billable": "output_image", "unit": "image", "cost_usd": 0.06},
        {"billable": "input_reference", "unit": "image", "cost_usd": 0.20},
    ]

    db.upsert_pricing(
        model_id="riverflow/riverflow-v2-pro",
        pricing=items,
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    row = db.get_pricing("riverflow/riverflow-v2-pro")

    assert row is not None
    assert row.pricing == items
    assert db.get_pricing("absent") is None


def test_upsert_pricing_overwrites(db):
    db.upsert_pricing(
        model_id="a/one",
        pricing=[{"billable": "output_image", "unit": "image", "cost_usd": 0.06}],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.upsert_pricing(
        model_id="a/one",
        pricing=[{"billable": "output_image", "unit": "image", "cost_usd": 0.08}],
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    row = db.get_pricing("a/one")

    assert row.pricing[0]["cost_usd"] == 0.08
    assert row.fetched_at == "2026-07-19T00:00:00+00:00"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_db_catalog_ledger.py -v`

Expected: FAIL — `AttributeError: 'Database' object has no attribute 'append_ledger'`

- [ ] **Step 3: Implement**

Append these methods to the `Database` class in `src/higgshole/store/db.py`, after `list_generation_children`:

```python
    # -- ledger (append-only) -------------------------------------------

    @staticmethod
    def _ledger(row: sqlite3.Row) -> LedgerRow:
        return LedgerRow(
            id=row["id"],
            generation_id=row["generation_id"],
            kind=LedgerKind(row["kind"]),
            amount=Decimal(row["amount"]),
            cost_known=bool(row["cost_known"]),
            recorded_at=row["recorded_at"],
        )

    def append_ledger(
        self,
        *,
        generation_id: str,
        kind: LedgerKind,
        amount: Decimal,
        cost_known: bool,
    ) -> LedgerRow:
        """Append one signed row. The ONLY write path into spend_ledger.

        The amount is stored as a Decimal literal in a TEXT column: SQLite's
        REAL would round money, and the sum of a day's rows is the number the
        spend cap is enforced against.
        """
        recorded_at = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO spend_ledger (generation_id, kind, amount,"
                " cost_known, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (
                    generation_id,
                    LedgerKind(kind).value,
                    str(amount),
                    1 if cost_known else 0,
                    recorded_at,
                ),
            )
            row_id = cursor.lastrowid

        return LedgerRow(
            id=int(row_id),
            generation_id=generation_id,
            kind=LedgerKind(kind),
            amount=amount,
            cost_known=cost_known,
            recorded_at=recorded_at,
        )

    def list_ledger_for_generation(self, gen_id: str) -> list[LedgerRow]:
        rows = self._conn.execute(
            "SELECT * FROM spend_ledger WHERE generation_id = ? ORDER BY id ASC",
            (gen_id,),
        ).fetchall()
        return [self._ledger(row) for row in rows]

    def list_ledger_between(self, *, start_iso: str, end_iso: str) -> list[LedgerRow]:
        """Rows with start_iso <= recorded_at < end_iso.

        Summing is the caller's job so that Decimal arithmetic never passes
        through SQLite (spec 3.3).
        """
        rows = self._conn.execute(
            "SELECT * FROM spend_ledger WHERE recorded_at >= ? AND recorded_at < ?"
            " ORDER BY id ASC",
            (start_iso, end_iso),
        ).fetchall()
        return [self._ledger(row) for row in rows]

    # -- catalogue ------------------------------------------------------

    @staticmethod
    def _catalog(row: sqlite3.Row) -> CatalogRow:
        return CatalogRow(
            model_id=row["model_id"],
            kind=GenerationKind(row["kind"]),
            capabilities=json.loads(row["capabilities"]),
            fetched_at=row["fetched_at"],
        )

    def upsert_catalog(
        self,
        *,
        model_id: str,
        kind: GenerationKind,
        capabilities: dict[str, Any],
        fetched_at: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO model_catalog (model_id, kind, capabilities, fetched_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(model_id, kind) DO UPDATE SET"
                " capabilities = excluded.capabilities,"
                " fetched_at = excluded.fetched_at",
                (
                    model_id,
                    GenerationKind(kind).value,
                    json.dumps(capabilities, sort_keys=True),
                    fetched_at,
                ),
            )

    def replace_catalog(
        self,
        kind: GenerationKind,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        fetched_at: str,
    ) -> None:
        """Replace the whole catalogue for one kind in a single transaction.

        A partially-fetched list must never half-overwrite a good cache, so
        the delete and the inserts share one transaction (spec 4.2).
        """
        value = GenerationKind(kind).value
        with self.transaction() as conn:
            conn.execute("DELETE FROM model_catalog WHERE kind = ?", (value,))
            conn.executemany(
                "INSERT INTO model_catalog (model_id, kind, capabilities, fetched_at)"
                " VALUES (?, ?, ?, ?)",
                [
                    (model_id, value, json.dumps(caps, sort_keys=True), fetched_at)
                    for model_id, caps in entries
                ],
            )

    def list_catalog(self, kind: GenerationKind | None = None) -> list[CatalogRow]:
        sql = "SELECT * FROM model_catalog"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(GenerationKind(kind).value)
        rows = self._conn.execute(sql + " ORDER BY model_id ASC", params).fetchall()
        return [self._catalog(row) for row in rows]

    def get_catalog(self, model_id: str, kind: GenerationKind) -> CatalogRow | None:
        row = self._conn.execute(
            "SELECT * FROM model_catalog WHERE model_id = ? AND kind = ?",
            (model_id, GenerationKind(kind).value),
        ).fetchone()
        return self._catalog(row) if row else None

    def catalog_fetched_at(self, kind: GenerationKind) -> str | None:
        """Oldest fetched_at across the kind, or None when the cache is empty.

        The oldest rather than the newest: freshness is only as good as the
        least fresh entry the operator might be shown.
        """
        row = self._conn.execute(
            "SELECT MIN(fetched_at) FROM model_catalog WHERE kind = ?",
            (GenerationKind(kind).value,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def upsert_pricing(
        self, *, model_id: str, pricing: list[dict[str, Any]], fetched_at: str
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO model_pricing (model_id, pricing, fetched_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(model_id) DO UPDATE SET"
                " pricing = excluded.pricing, fetched_at = excluded.fetched_at",
                (model_id, json.dumps(pricing), fetched_at),
            )

    def get_pricing(self, model_id: str) -> PricingRow | None:
        row = self._conn.execute(
            "SELECT * FROM model_pricing WHERE model_id = ?", (model_id,)
        ).fetchone()
        if row is None:
            return None
        return PricingRow(
            model_id=row["model_id"],
            pricing=json.loads(row["pricing"]),
            fetched_at=row["fetched_at"],
        )
```

Now remove the unused `field` import from the module header (it was only needed by the placeholder import block):

```python
from dataclasses import dataclass
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/ -v && uv run ruff check src/higgshole/store/`

Expected: PASS — `103 passed` for `tests/store/` (paths 28, files 17, schema 17, generations 28 — 90 before this task — plus the 13 added by the catalogue/ledger file), and `All checks passed!`

> Correct expectation: `tests/store/` now contains 28 + 17 + 17 + 28 + 13 = **103 tests**. Run `uv run pytest tests/store/test_db_catalog_ledger.py -v` alone to see `13 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/db.py tests/store/test_db_catalog_ledger.py
git commit -m "feat: add spend ledger, catalogue and pricing persistence"
```

---

## Task 6: Media probing and MIME helpers

**Files:**
- Modify: `pyproject.toml` (add `pillow`)
- Create: `src/higgshole/store/metadata.py`
- Test: `tests/store/test_metadata_probe.py`

**Interfaces:**
- Consumes: `store.files.file_size`.
- Produces: `MediaMetadata`, `probe_image`, `probe_video`, `probe_video_streams`, `probe_media`, `extension_for`, `mime_for`, `ffmpeg_available`, `UnsupportedMediaError`, `MetadataError`, and the private single subprocess seam `_run(args, *, timeout=60.0) -> subprocess.CompletedProcess[bytes]`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_metadata_probe.py`:

```python
import copy
import json
import shutil
import subprocess

import pytest
from PIL import Image

from higgshole.store.metadata import (
    MetadataError,
    UnsupportedMediaError,
    extension_for,
    ffmpeg_available,
    mime_for,
    probe_image,
    probe_media,
    probe_video,
    probe_video_streams,
)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not on PATH")

FFPROBE_OUTPUT = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "24/1",
        },
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {
        "duration": "8.000000",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "tags": {"comment": '{"higgshole": {"model": "google/veo-3.1"}}'},
    },
}


def write_png(path, size=(64, 32)):
    Image.new("RGB", size, (10, 20, 30)).save(path, format="PNG")
    return path


def fake_run(payload):
    def _run(args, *, timeout=60.0):
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout=payload, stderr=b""
        )

    return _run


def test_probe_image_reports_dimensions_and_mime(tmp_path):
    path = write_png(tmp_path / "a.png")

    meta = probe_image(path)

    assert meta.width == 64
    assert meta.height == 32
    assert meta.mime_type == "image/png"
    assert meta.duration_s is None
    assert meta.bytes == path.stat().st_size


def test_probe_image_reports_no_embedded_params_by_default(tmp_path):
    meta = probe_image(write_png(tmp_path / "a.png"))

    assert meta.embedded_params == {}


def test_probe_media_dispatches_to_the_image_branch(tmp_path):
    meta = probe_media(write_png(tmp_path / "a.png"))

    assert meta.mime_type == "image/png"


def test_probe_media_rejects_an_unsupported_type(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(UnsupportedMediaError):
        probe_media(path)


@pytest.mark.parametrize(
    ("mime", "ext"),
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/webp", "webp"),
        ("video/mp4", "mp4"),
        ("video/webm", "webm"),
    ],
)
def test_extension_for(mime, ext):
    assert extension_for(mime) == ext


def test_extension_for_rejects_unknown():
    with pytest.raises(UnsupportedMediaError):
        extension_for("application/pdf")


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.webp", "image/webp"),
        ("a.MP4", "video/mp4"),
    ],
)
def test_mime_for(tmp_path, name, mime):
    assert mime_for(tmp_path / name) == mime


def test_mime_for_rejects_unknown(tmp_path):
    with pytest.raises(UnsupportedMediaError):
        mime_for(tmp_path / "a.pdf")


def test_ffmpeg_available_reports_false_when_missing(monkeypatch):
    monkeypatch.setattr("higgshole.store.metadata.shutil.which", lambda name: None)

    assert ffmpeg_available() is False


def test_ffmpeg_available_reports_true_when_both_present(monkeypatch):
    monkeypatch.setattr(
        "higgshole.store.metadata.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    assert ffmpeg_available() is True


def test_probe_video_parses_ffprobe_output(tmp_path, monkeypatch):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(
        "higgshole.store.metadata._run",
        fake_run(json.dumps(FFPROBE_OUTPUT).encode()),
    )

    meta = probe_video(path)

    assert meta.width == 1280
    assert meta.height == 720
    assert meta.duration_s == pytest.approx(8.0)
    assert meta.mime_type == "video/mp4"
    assert meta.embedded_params == {"model": "google/veo-3.1"}


def test_probe_video_reads_uppercase_webm_comment_tag(tmp_path, monkeypatch):
    # The Matroska/WebM muxer uppercases tag keys, so a WebM written by
    # embed_video_params probes back as COMMENT, not comment.
    payload = copy.deepcopy(FFPROBE_OUTPUT)
    payload["format"]["format_name"] = "matroska,webm"
    payload["format"]["tags"] = {
        "COMMENT": '{"higgshole": {"model": "google/veo-3.1"}}',
        "ENCODER": "Lavf61.7.100",
    }
    path = tmp_path / "a.webm"
    path.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(
        "higgshole.store.metadata._run",
        fake_run(json.dumps(payload).encode()),
    )

    meta = probe_video(path)

    assert meta.mime_type == "video/webm"
    assert meta.embedded_params == {"model": "google/veo-3.1"}


def test_probe_video_raises_on_ffprobe_failure(tmp_path, monkeypatch):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"\x00" * 128)
    monkeypatch.setattr("higgshole.store.metadata._run", fake_run(b"not json"))

    with pytest.raises(MetadataError):
        probe_video(path)


def test_probe_video_streams_reports_fps_codec_and_audio(tmp_path, monkeypatch):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(
        "higgshole.store.metadata._run",
        fake_run(json.dumps(FFPROBE_OUTPUT).encode()),
    )

    streams = probe_video_streams(path)

    assert streams["codec"] == "h264"
    assert streams["fps"] == pytest.approx(24.0)
    assert streams["has_audio"] is True


@needs_ffmpeg
def test_probe_video_of_a_generated_file(tmp_path):
    # Generated at test time rather than committed: the repository carries no
    # binary media fixtures.
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=1:size=64x64:rate=10", "-y", str(path),
        ],
        check=True,
    )

    meta = probe_video(path)

    assert meta.width == 64
    assert meta.height == 64
    assert meta.duration_s == pytest.approx(1.0, abs=0.2)
    assert meta.bytes > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_metadata_probe.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'PIL'` (Pillow is not yet a dependency), and after installing it, `ModuleNotFoundError: No module named 'higgshole.store.metadata'`.

- [ ] **Step 3: Implement**

In `pyproject.toml`, extend the runtime dependencies:

```toml
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pillow>=10.3",
]
```

Run `uv sync --extra dev` to install it.

Create `src/higgshole/store/metadata.py`:

```python
"""What a media file itself reports, and what we write back into it.

Two external tools are involved: Pillow for stills and ffmpeg/ffprobe for
video. Every subprocess call goes through the single ``_run`` seam so that
tests stub one function rather than the whole of ``subprocess``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .files import file_size

#: The single tag name under which all parameters are embedded: PNG tEXt key,
#: EXIF UserComment payload prefix, and MP4 '-metadata comment=' value. One key
#: means rescan has one thing to look for regardless of container.
PARAM_TAG_KEY: str = "higgshole"

THUMBNAIL_MAX_EDGE: int = 512
THUMBNAIL_FORMAT: str = "webp"
POSTER_TIMESTAMP_S: float = 1.0

_MIME_BY_EXTENSION: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
}

#: The canonical extension for each MIME type. 'jpg' rather than 'jpeg' so a
#: round trip through mime_for/extension_for is stable.
_EXTENSION_BY_MIME: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
}

_PILLOW_FORMAT_TO_MIME: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

#: EXIF UserComment. Written into the base IFD rather than the Exif sub-IFD:
#: Pillow round-trips arbitrary base-IFD tags reliably across JPEG and WebP,
#: whereas mutating the sub-IFD depends on Pillow-version internals.
_EXIF_USER_COMMENT = 0x9286
_EXIF_PREFIX = b"ASCII\x00\x00\x00"


class UnsupportedMediaError(ValueError):
    """A media type neither the image nor the video branch handles."""


class MetadataError(RuntimeError):
    """ffprobe/ffmpeg failed or returned unparseable output."""


@dataclass(frozen=True)
class MediaMetadata:
    """What a file itself reports, independent of the database."""

    mime_type: str
    bytes: int
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    embedded_params: dict[str, Any] = field(default_factory=dict)


# -- the single subprocess seam -------------------------------------------


def _run(
    args: Sequence[str], *, timeout: float = 60.0
) -> subprocess.CompletedProcess[bytes]:
    """Run an external tool, raising MetadataError on any failure.

    Every ffmpeg/ffprobe invocation funnels through here so that tests replace
    one symbol, and so that a missing binary produces the same diagnosable
    error as a malformed input rather than a bare FileNotFoundError.
    """
    try:
        completed = subprocess.run(
            list(args), capture_output=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise MetadataError(f"{args[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MetadataError(f"{args[0]} timed out after {timeout}s") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise MetadataError(f"{args[0]} failed ({completed.returncode}): {detail}")
    return completed


def ffmpeg_available() -> bool:
    """Whether both ffmpeg and ffprobe are on PATH.

    Surfaced in Settings so a missing binary is diagnosed at a glance rather
    than at the first video job.
    """
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


# -- type helpers ---------------------------------------------------------


def extension_for(mime_type: str) -> str:
    """'image/png' -> 'png'. Raises UnsupportedMediaError for unknown types."""
    try:
        return _EXTENSION_BY_MIME[mime_type.split(";")[0].strip().lower()]
    except KeyError as exc:
        raise UnsupportedMediaError(f"unsupported media type: {mime_type}") from exc


def mime_for(path: Path) -> str:
    """Suffix-driven MIME lookup, restricted to the supported set."""
    suffix = Path(path).suffix.lstrip(".").lower()
    try:
        return _MIME_BY_EXTENSION[suffix]
    except KeyError as exc:
        raise UnsupportedMediaError(f"unsupported file extension: {path}") from exc


def _is_video(mime_type: str) -> bool:
    return mime_type.startswith("video/")


# -- probing --------------------------------------------------------------


def _decode_embedded(raw: object) -> dict[str, Any]:
    """Parse an embedded payload, returning {} for anything unusable.

    A corrupt tag must never fail a read: the sidecar is authoritative and
    embedding is a convenience (spec 5.3).
    """
    if isinstance(raw, bytes):
        if raw.startswith(_EXIF_PREFIX):
            raw = raw[len(_EXIF_PREFIX) :]
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get(PARAM_TAG_KEY, payload)
    return inner if isinstance(inner, dict) else {}


def probe_image(path: Path) -> MediaMetadata:
    """Dimensions, MIME type and any embedded HiggsHole parameters."""
    path = Path(path)
    with Image.open(path) as image:
        mime = _PILLOW_FORMAT_TO_MIME.get(image.format or "")
        if mime is None:
            raise UnsupportedMediaError(f"unsupported image format: {image.format}")
        width, height = image.size
        params = read_embedded_params_from_image(image)

    return MediaMetadata(
        mime_type=mime,
        bytes=file_size(path),
        width=width,
        height=height,
        duration_s=None,
        embedded_params=params,
    )


def read_embedded_params_from_image(image: Image.Image) -> dict[str, Any]:
    """Extract PARAM_TAG_KEY from an already-open Pillow image."""
    text = image.info.get(PARAM_TAG_KEY)
    if text:
        return _decode_embedded(text)

    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001 - a malformed EXIF block is not fatal
        return {}

    raw = exif.get(_EXIF_USER_COMMENT)
    if raw is None:
        raw = exif.get_ifd(0x8769).get(_EXIF_USER_COMMENT)
    return _decode_embedded(raw) if raw is not None else {}


def _ffprobe_json(path: Path) -> dict[str, Any]:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ]
    )
    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MetadataError(f"ffprobe returned unparseable output for {path}") from exc
    if not isinstance(payload, dict):
        raise MetadataError(f"ffprobe returned a non-object for {path}")
    return payload


def _video_stream(payload: dict[str, Any]) -> dict[str, Any]:
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return {}


def _parse_fps(raw: object) -> float | None:
    """ffprobe reports frame rates as the string 'num/den'."""
    if not isinstance(raw, str) or "/" not in raw:
        return None
    numerator, _, denominator = raw.partition("/")
    try:
        den = float(denominator)
        if den == 0:
            return None
        return float(numerator) / den
    except ValueError:
        return None


def probe_video(path: Path) -> MediaMetadata:
    """Dimensions, duration and embedded tags, via ffprobe."""
    path = Path(path)
    payload = _ffprobe_json(path)
    stream = _video_stream(payload)
    fmt = payload.get("format") or {}

    duration_raw = fmt.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None

    # Tag keys are case-sensitive in the ffprobe payload and muxers disagree on
    # case: MP4 writes `comment`, the Matroska/WebM muxer uppercases it to
    # `COMMENT`. Fold to lower case so WebM files written by embed_video_params
    # do not read back with their generation parameters silently missing.
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    comment = tags.get("comment")

    return MediaMetadata(
        mime_type=mime_for(path),
        bytes=file_size(path),
        width=stream.get("width"),
        height=stream.get("height"),
        duration_s=duration,
        embedded_params=_decode_embedded(comment),
    )


def probe_video_streams(path: Path) -> dict[str, Any]:
    """Codec, frame rate and audio presence.

    Kept separate from MediaMetadata because these fields have no column in
    `assets`; they exist for the detail view and for diagnostics.
    """
    payload = _ffprobe_json(Path(path))
    stream = _video_stream(payload)
    return {
        "codec": stream.get("codec_name"),
        "fps": _parse_fps(stream.get("avg_frame_rate")),
        "has_audio": any(
            s.get("codec_type") == "audio" for s in payload.get("streams") or []
        ),
    }


def probe_media(path: Path) -> MediaMetadata:
    """Dispatch on suffix. Raises UnsupportedMediaError for other types."""
    mime = mime_for(path)
    return probe_video(path) if _is_video(mime) else probe_image(path)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_metadata_probe.py -v`

Expected: PASS — `23 passed` (or `22 passed, 1 skipped` where ffmpeg is absent)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/higgshole/store/metadata.py tests/store/test_metadata_probe.py
git commit -m "feat: add media probing via Pillow and ffprobe"
```

---

## Task 7: Parameter embedding, thumbnails and poster frames

**Files:**
- Modify: `src/higgshole/store/metadata.py`
- Test: `tests/store/test_metadata_embed.py`

**Interfaces:**
- Consumes: `_run`, `PARAM_TAG_KEY`, `mime_for`, `probe_image`, `read_embedded_params_from_image`, `MetadataError` (Task 6); `store.files.atomic_write_bytes`, `store.files.delete_quietly`, `store.files.discard_part`.
- Produces: `embed_image_params`, `embed_video_params`, `embed_params`, `read_embedded_params`, `make_image_thumbnail`, `make_video_poster`, `make_video_thumbnail`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_metadata_embed.py`:

```python
import json
import shutil
import subprocess

import pytest
from PIL import Image, PngImagePlugin

from higgshole.store.metadata import (
    PARAM_TAG_KEY,
    THUMBNAIL_MAX_EDGE,
    embed_image_params,
    embed_params,
    embed_video_params,
    make_image_thumbnail,
    make_video_poster,
    make_video_thumbnail,
    probe_image,
    read_embedded_params,
)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not on PATH")

PAYLOAD = {
    "id": "a3f21c9d4e07",
    "model": "openai/gpt-image-2",
    "prompt": "neon city street at night, rain",
    "params": {"aspect_ratio": "16:9", "seed": 7},
}


def write_image(path, fmt, size=(120, 60)):
    Image.new("RGB", size, (10, 20, 30)).save(path, format=fmt)
    return path


def make_test_video(path, size="64x64", duration=1):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={size}:rate=10",
            "-y", str(path),
        ],
        check=True,
    )
    return path


def test_embed_and_read_png_params(tmp_path):
    path = write_image(tmp_path / "a.png", "PNG")

    embed_image_params(path, PAYLOAD)

    assert read_embedded_params(path) == PAYLOAD


def test_embed_and_read_jpeg_params(tmp_path):
    path = write_image(tmp_path / "a.jpg", "JPEG")

    embed_image_params(path, PAYLOAD)

    assert read_embedded_params(path) == PAYLOAD


def test_embed_image_params_preserves_dimensions(tmp_path):
    path = write_image(tmp_path / "a.png", "PNG", size=(200, 100))

    embed_image_params(path, PAYLOAD)

    meta = probe_image(path)
    assert (meta.width, meta.height) == (200, 100)
    assert meta.mime_type == "image/png"


def test_probe_image_surfaces_embedded_params(tmp_path):
    path = write_image(tmp_path / "a.png", "PNG")

    embed_image_params(path, PAYLOAD)

    assert probe_image(path).embedded_params == PAYLOAD


def test_read_embedded_params_returns_empty_when_absent(tmp_path):
    assert read_embedded_params(write_image(tmp_path / "a.png", "PNG")) == {}


def test_read_embedded_params_returns_empty_on_corrupt_payload(tmp_path):
    # A metadata failure must never fail a read: the sidecar is authoritative.
    path = tmp_path / "a.png"
    info = PngImagePlugin.PngInfo()
    info.add_text(PARAM_TAG_KEY, "{not json")
    Image.new("RGB", (10, 10)).save(path, format="PNG", pnginfo=info)

    assert read_embedded_params(path) == {}


def test_embed_params_dispatches_to_the_image_branch(tmp_path):
    path = write_image(tmp_path / "a.png", "PNG")

    embed_params(path, PAYLOAD)

    assert read_embedded_params(path) == PAYLOAD


def test_embed_video_params_stream_copies(tmp_path, monkeypatch):
    # Re-encoding a paid generation just to attach a tag would be
    # indefensible, so the ffmpeg invocation must carry -c copy.
    source = tmp_path / "a.mp4"
    source.write_bytes(b"\x00" * 64)
    seen = {}

    def fake_run(args, *, timeout=60.0):
        seen["args"] = list(args)
        # ffmpeg writes its output file; emulate that so the rename succeeds.
        open(args[-1], "wb").write(b"\x01" * 64)
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("higgshole.store.metadata._run", fake_run)

    embed_video_params(source, PAYLOAD)

    assert "-c" in seen["args"] and "copy" in seen["args"]
    metadata_arg = seen["args"][seen["args"].index("-metadata") + 1]
    assert json.loads(metadata_arg.split("comment=", 1)[1])[PARAM_TAG_KEY] == PAYLOAD
    assert source.read_bytes() == b"\x01" * 64


def test_make_image_thumbnail_bounds_the_long_edge(tmp_path):
    source = write_image(tmp_path / "a.png", "PNG", size=(2048, 1024))
    destination = tmp_path / "t.webp"

    meta = make_image_thumbnail(source, destination)

    assert max(meta.width, meta.height) <= THUMBNAIL_MAX_EDGE
    assert meta.width == THUMBNAIL_MAX_EDGE


def test_make_image_thumbnail_preserves_aspect_ratio(tmp_path):
    source = write_image(tmp_path / "a.png", "PNG", size=(2048, 1024))
    destination = tmp_path / "t.webp"

    meta = make_image_thumbnail(source, destination)

    assert meta.width / meta.height == pytest.approx(2.0, abs=0.02)


def test_make_image_thumbnail_writes_webp(tmp_path):
    source = write_image(tmp_path / "a.png", "PNG")
    destination = tmp_path / "t.webp"

    meta = make_image_thumbnail(source, destination)

    assert meta.mime_type == "image/webp"
    assert destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


@needs_ffmpeg
def test_embed_video_params_round_trip(tmp_path):
    path = make_test_video(tmp_path / "a.mp4")

    embed_video_params(path, PAYLOAD)

    assert read_embedded_params(path) == PAYLOAD


@needs_ffmpeg
def test_make_video_poster_extracts_a_frame(tmp_path):
    # The clip is shorter than POSTER_TIMESTAMP_S, exercising the fallback.
    source = make_test_video(tmp_path / "a.mp4", size="128x64", duration=1)
    destination = tmp_path / "poster.webp"

    meta = make_video_poster(source, destination)

    assert destination.exists()
    assert meta.mime_type == "image/webp"
    assert (meta.width, meta.height) == (128, 64)


@needs_ffmpeg
def test_make_video_thumbnail_bounds_the_long_edge(tmp_path):
    source = make_test_video(tmp_path / "a.mp4", size="1280x720", duration=1)
    destination = tmp_path / "t.webp"

    meta = make_video_thumbnail(source, destination)

    assert max(meta.width, meta.height) <= THUMBNAIL_MAX_EDGE
    assert not destination.with_name(destination.name + ".frame.webp").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_metadata_embed.py -v`

Expected: FAIL — `ImportError: cannot import name 'embed_image_params' from 'higgshole.store.metadata'`

- [ ] **Step 3: Implement**

Extend the imports at the top of `src/higgshole/store/metadata.py`:

```python
import io

from .files import atomic_write_bytes, delete_quietly, discard_part, file_size
```

Then append to the module:

```python
# -- embedding ------------------------------------------------------------


def _payload_json(payload: dict[str, Any]) -> str:
    """Compact JSON under a single top-level key.

    Wrapping in PARAM_TAG_KEY means the MP4 ``comment`` tag — which is shared
    with every other tool that writes comments — is unambiguously ours.
    """
    return json.dumps({PARAM_TAG_KEY: payload}, sort_keys=True, separators=(",", ":"))


def embed_image_params(path: Path, payload: dict[str, Any]) -> None:
    """Rewrite the image in place with parameters embedded (spec 5.3).

    PNG gets a tEXt chunk keyed PARAM_TAG_KEY; JPEG and WebP get an EXIF
    UserComment. The result is written via atomic_write_bytes, so a failure
    here never truncates the image that was just paid for.
    """
    path = Path(path)
    mime = mime_for(path)
    text = _payload_json(payload)
    buffer = io.BytesIO()

    with Image.open(path) as image:
        image.load()
        if mime == "image/png":
            info = PngImagePlugin.PngInfo()
            info.add_text(PARAM_TAG_KEY, text)
            image.save(buffer, format="PNG", pnginfo=info)
        elif mime in {"image/jpeg", "image/webp"}:
            exif = image.getexif()
            exif[_EXIF_USER_COMMENT] = _EXIF_PREFIX + text.encode("utf-8")
            image.save(
                buffer,
                format="JPEG" if mime == "image/jpeg" else "WEBP",
                exif=exif,
                quality=95,
            )
        else:
            raise UnsupportedMediaError(f"cannot embed parameters into {mime}")

    atomic_write_bytes(path, buffer.getvalue())


def embed_video_params(path: Path, payload: dict[str, Any]) -> None:
    """Remux with the parameters in the container comment tag.

    Stream-copy only (``-c copy``): re-encoding a paid generation to attach a
    tag would be indefensible. The muxer is named explicitly with ``-f``
    because the intermediate file is ``<name>.part``, whose extension ffmpeg
    cannot use to infer a format.
    """
    path = Path(path)
    mime = mime_for(path)
    if not _is_video(mime):
        raise UnsupportedMediaError(f"not a video: {path}")

    muxer = "mp4" if mime == "video/mp4" else "webm"
    part = path.with_name(path.name + ".part")

    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-c",
                "copy",
                "-map_metadata",
                "0",
                "-metadata",
                f"comment={_payload_json(payload)}",
                "-f",
                muxer,
                "-y",
                str(part),
            ]
        )
    except BaseException:
        discard_part(path)
        raise

    os.replace(part, path)


def embed_params(path: Path, payload: dict[str, Any]) -> None:
    """Dispatch by media type.

    A failure here is logged and swallowed by the caller: the sidecar is the
    authoritative record, embedding is a convenience, and a metadata failure
    must not fail a paid generation.
    """
    if _is_video(mime_for(path)):
        embed_video_params(path, payload)
    else:
        embed_image_params(path, payload)


def read_embedded_params(path: Path) -> dict[str, Any]:
    """Extract PARAM_TAG_KEY, returning {} when absent or unparseable."""
    path = Path(path)
    if _is_video(mime_for(path)):
        try:
            return probe_video(path).embedded_params
        except MetadataError:
            return {}
    with Image.open(path) as image:
        return read_embedded_params_from_image(image)


# -- thumbnails -----------------------------------------------------------


def make_image_thumbnail(
    source: Path, destination: Path, *, max_edge: int = THUMBNAIL_MAX_EDGE
) -> MediaMetadata:
    """Write a WebP thumbnail preserving aspect ratio; returns its metadata."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()

    with Image.open(source) as image:
        image.load()
        # P and CMYK have no direct WebP encoding; RGBA does and is preserved.
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image.save(buffer, format=THUMBNAIL_FORMAT.upper(), quality=82, method=4)

    atomic_write_bytes(destination, buffer.getvalue())
    return probe_image(destination)


def _extract_frame(source: Path, destination: Path, *, at_seconds: float) -> None:
    part = destination.with_name(destination.name + ".part")
    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{at_seconds:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-f",
                "webp",
                "-y",
                str(part),
            ]
        )
    except BaseException:
        discard_part(destination)
        raise
    os.replace(part, destination)


def make_video_poster(
    source: Path, destination: Path, *, at_seconds: float = POSTER_TIMESTAMP_S
) -> MediaMetadata:
    """Extract one frame as WebP.

    Falls back to timestamp 0 when the video is shorter than `at_seconds`:
    seeking past the end yields an empty output rather than an error on some
    ffmpeg builds, so the fallback is triggered by an unusable result as well
    as by a failure.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        _extract_frame(source, destination, at_seconds=at_seconds)
        if destination.exists() and file_size(destination) > 0:
            return probe_image(destination)
    except MetadataError:
        pass

    delete_quietly(destination)
    _extract_frame(source, destination, at_seconds=0.0)
    return probe_image(destination)


def make_video_thumbnail(
    source: Path,
    destination: Path,
    *,
    max_edge: int = THUMBNAIL_MAX_EDGE,
    at_seconds: float = POSTER_TIMESTAMP_S,
) -> MediaMetadata:
    """Poster frame downscaled to thumbnail size."""
    destination = Path(destination)
    frame = destination.with_name(destination.name + ".frame.webp")
    try:
        make_video_poster(source, frame, at_seconds=at_seconds)
        return make_image_thumbnail(frame, destination, max_edge=max_edge)
    finally:
        delete_quietly(frame)
```

Extend the Pillow import at the top of the module:

```python
from PIL import Image, PngImagePlugin
```

and add `import os` beside `import json`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_metadata_embed.py -v`

Expected: PASS — `14 passed` (or `11 passed, 3 skipped` where ffmpeg is absent)

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/metadata.py tests/store/test_metadata_embed.py
git commit -m "feat: embed generation parameters into media and build thumbnails"
```

---

## Task 8: Store package re-exports and the no-network invariant

**Files:**
- Modify: `src/higgshole/store/__init__.py`
- Test: `tests/store/test_package.py`

**Interfaces:**
- Consumes: everything in `store/`.
- Produces: the re-export surface every later plan imports from `higgshole.store`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_package.py`:

```python
import subprocess
import sys

from higgshole import store
from higgshole.store import (
    RESUMABLE_STATES,
    SIDECAR_VERSION,
    TERMINAL_STATES,
    AssetKind,
    AssetRow,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    LedgerKind,
    LedgerRow,
    MediaFilter,
    MediaMetadata,
    MediaPaths,
    PathTraversalError,
    ProjectRow,
    atomic_write_bytes,
    embed_params,
    new_id,
    probe_media,
    project_slug,
    read_sidecar,
    slugify,
    utc_now_iso,
    write_sidecar,
)


def test_re_exports_are_importable():
    exported = {
        RESUMABLE_STATES,
        SIDECAR_VERSION,
        TERMINAL_STATES,
        AssetKind,
        AssetRow,
        Database,
        ErrorReason,
        GenerationKind,
        GenerationRow,
        GenerationState,
        InputRole,
        LedgerKind,
        LedgerRow,
        MediaFilter,
        MediaMetadata,
        MediaPaths,
        PathTraversalError,
        ProjectRow,
    }

    assert len(exported) == 18
    for name in (
        atomic_write_bytes,
        embed_params,
        new_id,
        probe_media,
        project_slug,
        read_sidecar,
        slugify,
        utc_now_iso,
        write_sidecar,
    ):
        assert callable(name)
    assert set(store.__all__) >= {"Database", "MediaPaths", "utc_now_iso"}


def test_store_does_not_import_orclient():
    # Spec section 4.1: store/ touches disk and database only. Checked in a
    # fresh interpreter because this test session has already imported
    # orclient for other tests.
    script = (
        "import sys; import higgshole.store; "
        "leaked = [m for m in sys.modules if m.startswith('higgshole.orclient')]; "
        "assert not leaked, leaked"
    )

    result = subprocess.run([sys.executable, "-c", script], capture_output=True)

    assert result.returncode == 0, result.stderr.decode()


def test_enum_values_match_the_contract():
    assert GenerationKind.IMAGE.value == "image"
    assert GenerationState.DOWNLOADING.value == "DOWNLOADING"
    assert ErrorReason.PROVIDER_CANCELLED.value == "provider_cancelled"
    assert AssetKind.POSTER.value == "poster"
    assert InputRole.FIRST_FRAME.value == "first_frame"
    assert LedgerKind.REVERSAL.value == "reversal"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_package.py -v`

Expected: FAIL — `ImportError: cannot import name 'Database' from 'higgshole.store'`

- [ ] **Step 3: Implement**

Replace `src/higgshole/store/__init__.py`:

```python
"""Storage: paths, atomic writes, SQLite and media metadata.

This package touches the filesystem and the database and nothing else. It
never imports higgshole.orclient and never opens a socket (spec 4.1) — an
invariant a test in this package enforces in a fresh interpreter.
"""

from .db import (
    RESUMABLE_STATES,
    TERMINAL_STATES,
    AssetKind,
    AssetRow,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationRow,
    GenerationState,
    InputRole,
    LedgerKind,
    LedgerRow,
    MediaFilter,
    ProjectRow,
    utc_now_iso,
)
from .files import (
    SIDECAR_VERSION,
    atomic_write_bytes,
    read_sidecar,
    write_sidecar,
)
from .metadata import MediaMetadata, embed_params, probe_media
from .paths import MediaPaths, PathTraversalError, new_id, project_slug, slugify

__all__ = [
    "RESUMABLE_STATES",
    "SIDECAR_VERSION",
    "TERMINAL_STATES",
    "AssetKind",
    "AssetRow",
    "Database",
    "ErrorReason",
    "GenerationKind",
    "GenerationRow",
    "GenerationState",
    "InputRole",
    "LedgerKind",
    "LedgerRow",
    "MediaFilter",
    "MediaMetadata",
    "MediaPaths",
    "PathTraversalError",
    "ProjectRow",
    "atomic_write_bytes",
    "embed_params",
    "new_id",
    "probe_media",
    "project_slug",
    "read_sidecar",
    "slugify",
    "utc_now_iso",
    "write_sidecar",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/ -v && uv run ruff check .`

Expected: PASS — `143 passed` for `tests/store/` (28 + 17 + 17 + 28 + 13 + 23 + 14 + 3, minus any ffmpeg skips), and `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/store/__init__.py tests/store/test_package.py
git commit -m "feat: publish the store package surface and enforce its boundary"
```

---

## Task 9: Catalogue cache, TTL refresh and lazy image pricing

**Files:**
- Modify: `pyproject.toml` (add `anyio`)
- Create: `src/higgshole/catalog/cache.py`
- Modify: `src/higgshole/catalog/__init__.py` (add the cache re-exports)
- Test: `tests/catalog/test_cache.py`

**Interfaces:**
- Consumes: `orclient.OpenRouterClient`, `orclient.VideoModel`, `orclient.ImageModel`, `orclient.OpenRouterError` (Plan 1); `store.db.Database`, `GenerationKind`, `utc_now_iso` (Tasks 3–5); `config.Settings`.
- Produces: `CatalogStatus`, `CatalogCache`, and the two payload serialisers `video_capabilities(model) -> dict` and `image_capabilities(model) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/catalog/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/catalog/test_cache.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.catalog.cache'`

- [ ] **Step 3: Implement**

In `pyproject.toml`, add `anyio` to the runtime dependencies (it arrives transitively with `httpx`, but the periodic-refresh task depends on it directly, so it is declared):

```toml
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pillow>=10.3",
    "anyio>=4.4",
]
```

Run `uv sync --extra dev`.

Create `src/higgshole/catalog/cache.py`:

```python
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
```

Extend `src/higgshole/catalog/__init__.py`:

```python
"""Model capability catalogue, caching and request validation."""

from .cache import CatalogCache, CatalogStatus, image_capabilities, video_capabilities
from .validation import (
    Severity,
    ValidationIssue,
    has_hard_failure,
    validate_image_request,
    validate_video_request,
)

__all__ = [
    "CatalogCache",
    "CatalogStatus",
    "Severity",
    "ValidationIssue",
    "has_hard_failure",
    "image_capabilities",
    "validate_image_request",
    "validate_video_request",
    "video_capabilities",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/catalog/test_cache.py -v`

Expected: PASS — `19 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/higgshole/catalog/ tests/catalog/test_cache.py
git commit -m "feat: add catalogue cache with TTL refresh and lazy image pricing"
```

---

## Task 10: Video cost estimation from `pricing_skus`

**Files:**
- Create: `src/higgshole/budget/__init__.py` (empty for now; Task 13 populates it)
- Create: `src/higgshole/budget/estimator.py`
- Create: `tests/budget/__init__.py` (empty)
- Test: `tests/budget/test_estimator_video.py`

**Interfaces:**
- Consumes: `orclient.types.VideoModel` (Plan 1 Task 4).
- Produces: `EstimateUnavailable`, `Estimate`, `CENTS_PREFIX`, `TOKEN_UNITS`, `parse_sku_amount`, `estimate_video_cost`, `reservation_amount`.

The SKU payloads below are the real shapes described in spec §3.1. They are the whole reason this module exists: for roughly 40–50% of the video catalogue there is no correct number to compute, and the only defensible answer is `None`.

- [ ] **Step 1: Write the failing test**

Create an empty `tests/budget/__init__.py`, then create `tests/budget/test_estimator_video.py`:

```python
from decimal import Decimal

import pytest

from higgshole.budget.estimator import (
    Estimate,
    EstimateUnavailable,
    estimate_video_cost,
    parse_sku_amount,
    reservation_amount,
)
from higgshole.orclient.types import VideoModel

# Spec section 3.1 item 3: mode- and resolution-qualified SKUs with no audio
# variants, alongside a bare with-audio SKU carrying a 50% surcharge.
KLING = VideoModel.from_api(
    {
        "id": "kwaivgi/kling-v3.0-pro",
        "supported_resolutions": ["720p"],
        "supported_durations": [5, 10],
        "supported_frame_images": ["first_frame", "last_frame"],
        "generate_audio": True,
        "pricing_skus": {
            "text_to_video_duration_seconds_480p": "0.028",
            "text_to_video_duration_seconds_720p": "0.056",
            "text_to_video_duration_seconds_1080p": "0.112",
            "image_to_video_duration_seconds_480p": "0.028",
            "image_to_video_duration_seconds_720p": "0.056",
            "image_to_video_duration_seconds_1080p": "0.112",
            "duration_seconds_with_audio": "0.168",
        },
    }
)

VEO = VideoModel.from_api(
    {
        "id": "google/veo-3.1",
        "supported_durations": [4, 6, 8],
        "generate_audio": True,
        "pricing_skus": {"duration_seconds_with_audio": "0.40"},
    }
)

# Spec section 3.1 item 4: audio-capable, bare SKU only.
WAN = VideoModel.from_api(
    {
        "id": "alibaba/wan-2.7",
        "supported_durations": [5],
        "generate_audio": True,
        "pricing_skus": {"duration_seconds": "0.050"},
    }
)

SILENT = VideoModel.from_api(
    {
        "id": "alibaba/happyhorse-1",
        "supported_durations": [5],
        "generate_audio": None,
        "pricing_skus": {"duration_seconds": "0.050"},
    }
)

# Spec section 3.1 item 1: no published tokens-per-second table.
SEEDANCE = VideoModel.from_api(
    {
        "id": "bytedance/seedance-1.5-pro",
        "supported_durations": [5, 10],
        "pricing_skus": {"video_tokens": "0.000007"},
    }
)

# Spec section 3.1 item 2: reading "7" as dollars is a 100x overestimate.
GROK = VideoModel.from_api(
    {
        "id": "x-ai/grok-imagine-video",
        "supported_durations": [5],
        "pricing_skus": {"cents_per_duration_seconds": "7"},
    }
)

# Spec section 3.1 item 5: the guide's hyphenated grammar matches no live model.
DOCUMENTED_BUT_UNREAL = VideoModel.from_api(
    {"id": "example/from-the-guide", "pricing_skus": {"per-video-second": "0.10"}}
)

UNPRICED = VideoModel.from_api({"id": "example/unpriced", "supported_durations": [5]})

TIED = VideoModel.from_api(
    {
        "id": "example/tied",
        "supported_durations": [5],
        "pricing_skus": {"duration_seconds": "0.10", "duration_seconds_pro": "0.20"},
    }
)


def test_veo_audio_only_sku_is_exact():
    estimate = estimate_video_cost(VEO, duration=8, generate_audio=True)

    assert estimate.amount == Decimal("3.20")
    assert estimate.reason is None
    assert estimate.sku_key == "duration_seconds_with_audio"


def test_kling_image_to_video_720p_without_audio_is_exact():
    estimate = estimate_video_cost(
        KLING, duration=5, resolution="720p", generate_audio=False, has_frame_images=True
    )

    assert estimate.amount == Decimal("0.28")
    assert estimate.sku_key == "image_to_video_duration_seconds_720p"


def test_kling_audio_with_resolution_is_ambiguous():
    # The audio SKU is unqualified while the mode/resolution SKUs carry no
    # audio variant. Most-specific-match would silently drop a 50% surcharge.
    estimate = estimate_video_cost(
        KLING, duration=5, resolution="720p", generate_audio=True, has_frame_images=True
    )

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES


def test_kling_text_to_video_is_exact():
    estimate = estimate_video_cost(KLING, duration=5, resolution="1080p")

    assert estimate.amount == Decimal("0.56")
    assert estimate.sku_key == "text_to_video_duration_seconds_1080p"


def test_audio_capable_model_with_only_a_bare_sku_is_missing_axis():
    estimate = estimate_video_cost(WAN, duration=5, generate_audio=True)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_seedance_video_tokens_are_not_estimable():
    estimate = estimate_video_cost(SEEDANCE, duration=5)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.VIDEO_TOKEN_PRICED


def test_grok_cents_per_prefix_divides_by_one_hundred():
    estimate = estimate_video_cost(GROK, duration=5)

    assert estimate.amount == Decimal("0.35")


def test_grok_is_not_read_as_dollars():
    estimate = estimate_video_cost(GROK, duration=5)

    assert estimate.amount != Decimal("35")


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("duration_seconds", "0.112", Decimal("0.112")),
        ("cents_per_duration_seconds", "7", Decimal("0.07")),
        ("cents_per_video_second", "0.5", Decimal("0.005")),
        ("duration_seconds_with_audio", "0.40", Decimal("0.40")),
    ],
)
def test_parse_sku_amount(key, raw, expected):
    assert parse_sku_amount(key, raw) == expected


def test_a_model_with_no_pricing_skus_reports_no_pricing_data():
    estimate = estimate_video_cost(UNPRICED, duration=5)

    assert estimate.reason is EstimateUnavailable.NO_PRICING_DATA


def test_an_unknown_unit_is_reported_as_such():
    estimate = estimate_video_cost(DOCUMENTED_BUT_UNREAL, duration=5)

    assert estimate.reason is EstimateUnavailable.UNKNOWN_UNIT


def test_a_missing_duration_is_a_missing_axis():
    estimate = estimate_video_cost(VEO, duration=None, generate_audio=True)

    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_an_unpriced_resolution_has_no_matching_sku():
    estimate = estimate_video_cost(KLING, duration=5, resolution="4K")

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_audio_requested_on_a_non_audio_model_has_no_matching_sku():
    estimate = estimate_video_cost(SILENT, duration=5, generate_audio=True)

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_estimate_amount_and_reason_are_mutually_exclusive():
    for estimate in (
        estimate_video_cost(VEO, duration=8, generate_audio=True),
        estimate_video_cost(SEEDANCE, duration=5),
        estimate_video_cost(UNPRICED, duration=5),
    ):
        assert (estimate.amount is None) != (estimate.reason is None)
        assert estimate.detail


def test_is_exact_reflects_the_amount():
    assert estimate_video_cost(VEO, duration=8, generate_audio=True).is_exact is True
    assert estimate_video_cost(SEEDANCE, duration=5).is_exact is False


def test_reservation_amount_uses_an_exact_estimate():
    estimate = estimate_video_cost(VEO, duration=8, generate_audio=True)

    assert reservation_amount(estimate, max_job_cost_usd=Decimal("2.00")) == (
        Decimal("3.20"),
        True,
    )


def test_reservation_amount_falls_back_to_the_ceiling():
    # Spec section 3.3: a non-estimable job reserves the pessimistic ceiling.
    estimate = estimate_video_cost(SEEDANCE, duration=5)

    assert reservation_amount(estimate, max_job_cost_usd=Decimal("2.00")) == (
        Decimal("2.00"),
        False,
    )


def test_two_equally_specific_conflicting_skus_are_ambiguous():
    estimate = estimate_video_cost(TIED, duration=5)

    assert isinstance(estimate, Estimate)
    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/budget/test_estimator_video.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.budget'`

- [ ] **Step 3: Implement**

Create an empty `src/higgshole/budget/__init__.py`.

Create `src/higgshole/budget/estimator.py`:

```python
"""Advisory pre-flight cost estimation.

Spec section 3.1 established that pre-flight estimation is unreliable for
roughly 40-50% of the video catalogue. This module's job is therefore as much
to *refuse* as to compute: every path that cannot resolve to exactly one SKU
returns ``Estimate(amount=None, reason=...)``. A wrong number here would be
displayed as a price and reserved against the daily cap, so a fabricated
figure is worse than no figure at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from higgshole.orclient.types import ImageModel, VideoModel

#: x-ai/grok-imagine-video prefixes its SKU keys with this. Reading '7' as
#: dollars is a 100x overestimate (spec 3.1 item 2).
CENTS_PREFIX: str = "cents_per_"

#: Units with no published conversion table. Always yields an estimate of
#: None — never a guess (spec 3.1 items 1 and 4).
TOKEN_UNITS: frozenset[str] = frozenset({"token", "video_tokens"})

_MODE_PREFIXES = ("text_to_video_", "image_to_video_")
_AUDIO_MARKER = "_with_audio"
_DURATION_MARKER = "duration_seconds"
_RESOLUTION_RE = re.compile(r"^\d+p$")
_CENTS_DIVISOR = Decimal(100)
_QUANTUM = Decimal("0.000001")


class EstimateUnavailable(StrEnum):
    """Machine-readable reasons an estimate cannot be computed (spec 3.2)."""

    TOKEN_PRICED = "token_priced"
    VIDEO_TOKEN_PRICED = "video_token_priced"
    NO_MATCHING_SKU = "no_matching_sku"
    AMBIGUOUS_AXES = "ambiguous_axes"
    MISSING_AXIS = "missing_axis"
    UNKNOWN_UNIT = "unknown_unit"
    NO_PRICING_DATA = "no_pricing_data"


@dataclass(frozen=True)
class Estimate:
    """An advisory pre-flight cost.

    `amount` is None whenever `reason` is set; the two are never both
    populated and never both empty.
    """

    amount: Decimal | None
    reason: EstimateUnavailable | None
    detail: str
    sku_key: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.amount is not None


def _unavailable(reason: EstimateUnavailable, detail: str) -> Estimate:
    return Estimate(amount=None, reason=reason, detail=detail)


def _quantise(value: Decimal) -> Decimal:
    """Six decimal places: sub-cent SKUs are real, sub-microdollar ones are not."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def parse_sku_amount(key: str, raw: str) -> Decimal:
    """Convert one SKU value to USD, dividing by 100 for CENTS_PREFIX keys."""
    value = Decimal(str(raw))
    return value / _CENTS_DIVISOR if key.startswith(CENTS_PREFIX) else value


def _base_key(key: str) -> str:
    return key[len(CENTS_PREFIX) :] if key.startswith(CENTS_PREFIX) else key


def _unit_family(base: str) -> str:
    if "video_tokens" in base or base == "token" or base.endswith("_tokens"):
        return "token"
    if _DURATION_MARKER in base:
        return "duration"
    return "unknown"


def _mode_of(base: str) -> str | None:
    for prefix in _MODE_PREFIXES:
        if base.startswith(prefix):
            return prefix
    return None


def _resolution_of(base: str) -> str | None:
    tail = base.rsplit("_", 1)[-1]
    return tail if _RESOLUTION_RE.match(tail) else None


@dataclass(frozen=True)
class _Match:
    key: str | None
    score: int
    ambiguous: bool


def _best_match(
    keys: list[str],
    skus: dict[str, str],
    *,
    wanted_prefix: str,
    resolution: str | None,
) -> _Match:
    """Pick the most specific SKU whose axes do not contradict the request.

    A key qualified with the *other* mode, or with a different resolution, is
    a contradiction and is discarded. A resolution-qualified key is discarded
    when no resolution was requested, because choosing one arbitrarily would
    invent an axis value the caller never supplied.
    """
    scored: list[tuple[int, str]] = []
    for key in keys:
        base = _base_key(key)
        mode = _mode_of(base)
        found = _resolution_of(base)

        if mode is not None and mode != wanted_prefix:
            continue
        if found is not None and found != resolution:
            continue

        score = (1 if mode == wanted_prefix else 0) + (
            1 if resolution is not None and found == resolution else 0
        )
        scored.append((score, key))

    if not scored:
        return _Match(key=None, score=-1, ambiguous=False)

    top = max(score for score, _ in scored)
    winners = [key for score, key in scored if score == top]
    amounts = {parse_sku_amount(key, skus[key]) for key in winners}
    return _Match(key=winners[0], score=top, ambiguous=len(amounts) > 1)


def estimate_video_cost(
    model: VideoModel,
    *,
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool = False,
    has_frame_images: bool = False,
) -> Estimate:
    """Resolve pricing_skus for the requested axes.

    Returns Estimate(amount=None, reason=...) whenever the axes do not resolve
    to exactly one SKU. In particular, an audio-capable model whose SKU set
    lacks an audio variant yields MISSING_AXIS rather than the non-audio
    price, and a model whose audio SKU is less specific than its non-audio
    SKUs yields AMBIGUOUS_AXES: most-specific-match would silently drop
    Kling's 50% audio surcharge (spec 3.1 item 3).

    `aspect_ratio` is accepted for interface symmetry; no live model prices on
    that axis, so it never participates in SKU selection.
    """
    skus = dict(model.pricing_skus)
    if not skus:
        return _unavailable(
            EstimateUnavailable.NO_PRICING_DATA,
            f"{model.id} publishes no pricing SKUs.",
        )

    families = {key: _unit_family(_base_key(key)) for key in skus}
    duration_keys = [key for key, family in families.items() if family == "duration"]

    if not duration_keys:
        if all(family == "token" for family in families.values()):
            return _unavailable(
                EstimateUnavailable.VIDEO_TOKEN_PRICED,
                f"{model.id} is priced per video token, and no tokens-per-second "
                "table is published, so no cost can be computed before dispatch.",
            )
        return _unavailable(
            EstimateUnavailable.UNKNOWN_UNIT,
            f"{model.id} prices in an unrecognised unit: {', '.join(sorted(skus))}.",
        )

    if duration is None:
        return _unavailable(
            EstimateUnavailable.MISSING_AXIS,
            f"{model.id} prices per second, but no duration was supplied.",
        )

    wanted_prefix = _MODE_PREFIXES[1] if has_frame_images else _MODE_PREFIXES[0]
    audio_keys = [
        key
        for key in duration_keys
        if (_AUDIO_MARKER in _base_key(key)) == generate_audio
    ]

    if not audio_keys:
        if generate_audio and model.generate_audio:
            return _unavailable(
                EstimateUnavailable.MISSING_AXIS,
                f"{model.id} generates audio but publishes no with-audio SKU, so "
                "the audio surcharge cannot be priced.",
            )
        return _unavailable(
            EstimateUnavailable.NO_MATCHING_SKU,
            f"{model.id} has no SKU matching generate_audio={generate_audio}.",
        )

    best = _best_match(
        audio_keys, skus, wanted_prefix=wanted_prefix, resolution=resolution
    )
    if best.key is None:
        return _unavailable(
            EstimateUnavailable.NO_MATCHING_SKU,
            f"{model.id} publishes no SKU for resolution={resolution} with "
            f"{'image' if has_frame_images else 'text'}-to-video.",
        )
    if best.ambiguous:
        return _unavailable(
            EstimateUnavailable.AMBIGUOUS_AXES,
            f"{model.id} has several equally specific SKUs at different prices; "
            "picking one would be a guess.",
        )

    if generate_audio:
        # If dropping the audio axis would have produced a strictly more
        # specific match, the axes are non-orthogonal and neither candidate is
        # correct — this is exactly the Kling case (spec 3.1 item 3).
        rival = _best_match(
            [key for key in duration_keys if _AUDIO_MARKER not in _base_key(key)],
            skus,
            wanted_prefix=wanted_prefix,
            resolution=resolution,
        )
        if rival.score > best.score:
            return _unavailable(
                EstimateUnavailable.AMBIGUOUS_AXES,
                f"{model.id} prices audio and resolution on separate, "
                "non-orthogonal SKU axes; no single SKU covers this request.",
            )

    amount = _quantise(parse_sku_amount(best.key, skus[best.key]) * duration)
    return Estimate(
        amount=amount,
        reason=None,
        detail=f"{duration}s at {skus[best.key]} per second ({best.key}).",
        sku_key=best.key,
    )


def reservation_amount(
    estimate: Estimate, *, max_job_cost_usd: Decimal
) -> tuple[Decimal, bool]:
    """The amount to reserve, and whether it came from an exact estimate.

    Exactly estimable -> (estimate.amount, True). Otherwise the pessimistic
    ceiling (spec 3.3): the cap must over-count rather than under-count, since
    under-counting lets it silently never trip.
    """
    if estimate.amount is not None:
        return estimate.amount, True
    return max_job_cost_usd, False
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/budget/test_estimator_video.py -v`

Expected: PASS — `22 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/budget/ tests/budget/
git commit -m "feat: add video cost estimation with explicit non-estimable reasons"
```

---

## Task 11: Image cost estimation from line items

**Files:**
- Modify: `src/higgshole/budget/estimator.py`
- Test: `tests/budget/test_estimator_image.py`

**Interfaces:**
- Consumes: `orclient.types.ImageModel`, and the `Estimate`/`EstimateUnavailable`/`_quantise`/`_unavailable` helpers from Task 10.
- Produces: `estimate_image_cost(model, pricing, *, width=None, height=None, quality=None, reference_count=0) -> Estimate`.

Image pricing arrives as an **array of line items** (`{billable, unit, cost_usd, variant?}`) from `/images/models/{id}/endpoints`, not a map. Input-side items are material: `input_reference` on `riverflow-v2-pro` costs $0.20 each, so five references add $1.00 before generation (spec §3.2).

- [ ] **Step 1: Write the failing test**

Create `tests/budget/test_estimator_image.py`:

```python
from decimal import Decimal

from higgshole.budget.estimator import EstimateUnavailable, estimate_image_cost
from higgshole.orclient.types import ImageModel

RECRAFT = ImageModel.from_api(
    {
        "id": "recraft/recraft-v4.1",
        "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 1}},
    }
)

RIVERFLOW = ImageModel.from_api(
    {
        "id": "riverflow/riverflow-v2-pro",
        "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 5}},
    }
)

FLUX = ImageModel.from_api({"id": "black-forest-labs/flux.2-pro"})

GPT_IMAGE = ImageModel.from_api(
    {
        "id": "openai/gpt-image-2",
        "supported_parameters": {
            "quality": {"type": "enum", "values": ["auto", "low", "medium", "high"]}
        },
    }
)

FLAT = [{"billable": "output_image", "unit": "image", "cost_usd": 0.04}]

RIVERFLOW_PRICING = [
    {"billable": "output_image", "unit": "image", "cost_usd": 0.06},
    {"billable": "input_reference", "unit": "image", "cost_usd": 0.20},
]

MEGAPIXEL = [{"billable": "output_image", "unit": "megapixel", "cost_usd": 0.03}]

# Spec section 3.1: OpenAI GPT-Image, Gemini and MAI price in tokens.
TOKEN = [{"billable": "output_image", "unit": "token", "cost_usd": 3e-05}]

VARIANTS = [
    {"billable": "output_image", "unit": "image", "cost_usd": 0.04, "variant": "standard"},
    {"billable": "output_image", "unit": "image", "cost_usd": 0.08, "variant": "hd"},
]

WEIRD = [{"billable": "output_image", "unit": "furlong", "cost_usd": 0.04}]


def test_recraft_flat_image_price_is_exact():
    estimate = estimate_image_cost(RECRAFT, FLAT)

    assert estimate.amount == Decimal("0.04")
    assert estimate.reason is None


def test_riverflow_input_references_are_added():
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=1)

    assert estimate.amount == Decimal("0.26")


def test_five_references_add_a_dollar():
    # Spec section 3.2: input-side items are material and must be included.
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=5)

    assert estimate.amount == Decimal("1.06")


def test_megapixel_pricing_uses_the_requested_dimensions():
    estimate = estimate_image_cost(FLUX, MEGAPIXEL, width=1920, height=1080)

    assert estimate.amount == Decimal("0.062208")


def test_megapixel_pricing_without_dimensions_is_a_missing_axis():
    estimate = estimate_image_cost(FLUX, MEGAPIXEL)

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.MISSING_AXIS


def test_token_priced_models_are_not_estimable():
    estimate = estimate_image_cost(GPT_IMAGE, TOKEN, quality="high")

    assert estimate.amount is None
    assert estimate.reason is EstimateUnavailable.TOKEN_PRICED


def test_quality_variants_select_the_matching_line_item():
    estimate = estimate_image_cost(RECRAFT, VARIANTS, quality="hd")

    assert estimate.amount == Decimal("0.08")


def test_an_unmatched_quality_variant_has_no_matching_sku():
    estimate = estimate_image_cost(RECRAFT, VARIANTS, quality="ultra")

    assert estimate.reason is EstimateUnavailable.NO_MATCHING_SKU


def test_ambiguous_variants_without_a_quality_are_reported():
    estimate = estimate_image_cost(RECRAFT, VARIANTS)

    assert estimate.reason is EstimateUnavailable.AMBIGUOUS_AXES


def test_empty_pricing_reports_no_pricing_data():
    estimate = estimate_image_cost(RECRAFT, [])

    assert estimate.reason is EstimateUnavailable.NO_PRICING_DATA


def test_an_unknown_unit_is_reported():
    estimate = estimate_image_cost(RECRAFT, WEIRD)

    assert estimate.reason is EstimateUnavailable.UNKNOWN_UNIT


def test_zero_references_add_nothing():
    estimate = estimate_image_cost(RIVERFLOW, RIVERFLOW_PRICING, reference_count=0)

    assert estimate.amount == Decimal("0.06")


def test_estimate_never_returns_a_fabricated_zero():
    # Spec section 3.4: zero would let the daily cap silently never trip.
    for estimate in (
        estimate_image_cost(GPT_IMAGE, TOKEN),
        estimate_image_cost(RECRAFT, []),
        estimate_image_cost(FLUX, MEGAPIXEL),
    ):
        assert estimate.amount is None
        assert estimate.amount != Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/budget/test_estimator_image.py -v`

Expected: FAIL — `ImportError: cannot import name 'estimate_image_cost' from 'higgshole.budget.estimator'`

- [ ] **Step 3: Implement**

Append to `src/higgshole/budget/estimator.py`:

```python
# -- image pricing --------------------------------------------------------

#: Line items describing the generated image itself.
_OUTPUT_BILLABLES = frozenset({"output_image", "image", "output"})

#: Line items charged per supplied reference image.
_INPUT_BILLABLES = frozenset({"input_reference", "input_image"})

_PER_MEGAPIXEL = Decimal(1_000_000)


def _line_unit(item: dict[str, Any]) -> str:
    return str(item.get("unit") or "").strip().lower()


def _line_cost(item: dict[str, Any]) -> Decimal:
    return Decimal(str(item.get("cost_usd")))


def estimate_image_cost(
    model: ImageModel,
    pricing: list[dict[str, Any]],
    *,
    width: int | None = None,
    height: int | None = None,
    quality: str | None = None,
    reference_count: int = 0,
) -> Estimate:
    """Sum the matching line items from /images/models/{id}/endpoints.

    Input-side items are material and must be included: input_reference on
    riverflow-v2-pro costs $0.20 each, so five references add $1.00 before a
    single pixel is generated (spec 3.2).
    """
    if not pricing:
        return _unavailable(
            EstimateUnavailable.NO_PRICING_DATA,
            f"no cached pricing for {model.id}.",
        )

    outputs = [
        item for item in pricing if str(item.get("billable")) in _OUTPUT_BILLABLES
    ]
    if not outputs:
        return _unavailable(
            EstimateUnavailable.NO_MATCHING_SKU,
            f"{model.id} publishes no output line item.",
        )

    if any(_line_unit(item) in TOKEN_UNITS for item in outputs):
        return _unavailable(
            EstimateUnavailable.TOKEN_PRICED,
            f"{model.id} is billed per token, and no tokens-per-image table is "
            "published, so no cost can be computed before dispatch.",
        )

    variants = [item for item in outputs if item.get("variant")]
    if quality is not None and variants:
        matched = [item for item in variants if str(item["variant"]) == quality]
        if not matched:
            return _unavailable(
                EstimateUnavailable.NO_MATCHING_SKU,
                f"{model.id} publishes no line item for quality={quality}.",
            )
        chosen = matched[0]
    elif len(outputs) == 1:
        chosen = outputs[0]
    else:
        return _unavailable(
            EstimateUnavailable.AMBIGUOUS_AXES,
            f"{model.id} publishes several output line items and no quality was "
            "given to choose between them.",
        )

    unit = _line_unit(chosen)
    if unit == "image":
        total = _line_cost(chosen)
    elif unit == "megapixel":
        if width is None or height is None:
            return _unavailable(
                EstimateUnavailable.MISSING_AXIS,
                f"{model.id} is priced per megapixel, but no output dimensions "
                "were supplied.",
            )
        total = _line_cost(chosen) * (Decimal(width * height) / _PER_MEGAPIXEL)
    else:
        return _unavailable(
            EstimateUnavailable.UNKNOWN_UNIT,
            f"{model.id} prices its output in an unrecognised unit: {unit}.",
        )

    if reference_count:
        for item in pricing:
            if str(item.get("billable")) not in _INPUT_BILLABLES:
                continue
            if _line_unit(item) in TOKEN_UNITS:
                return _unavailable(
                    EstimateUnavailable.TOKEN_PRICED,
                    f"{model.id} bills reference images per token, which cannot "
                    "be converted before dispatch.",
                )
            total += _line_cost(item) * reference_count

    return Estimate(
        amount=_quantise(total),
        reason=None,
        detail=(
            f"{chosen.get('billable')} at {chosen.get('cost_usd')} per {unit}"
            + (f" plus {reference_count} reference(s)" if reference_count else "")
            + "."
        ),
        sku_key=str(chosen.get("billable")),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/budget/ -v && uv run ruff check src/higgshole/budget/`

Expected: PASS — `35 passed` (video 22 + image 13), and `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/budget/estimator.py tests/budget/test_estimator_image.py
git commit -m "feat: add image cost estimation from pricing line items"
```

---

## Task 12: The append-only spend ledger

**Files:**
- Create: `src/higgshole/budget/ledger.py`
- Test: `tests/budget/test_ledger.py`

**Interfaces:**
- Consumes: `store.db.Database`, `LedgerKind`, `LedgerRow` (Tasks 3–5).
- Produces: `DaySpend`, `BudgetStatus`, `Ledger` (including the read-only `Ledger.db -> Database` property that later plans use for raw ledger reads during reservation recovery), `utc_day_bounds(day: date) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/budget/test_ledger.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from higgshole.budget.ledger import Ledger, utc_day_bounds
from higgshole.store.db import Database, GenerationKind, LedgerKind


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def gen_id(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    ).id


@pytest.fixture
def ledger(db):
    return Ledger(db)


def freeze(monkeypatch, iso):
    monkeypatch.setattr("higgshole.store.db.utc_now_iso", lambda: iso)


def test_reserve_appends_a_positive_row(ledger, gen_id):
    row = ledger.reserve(gen_id, Decimal("2.00"))

    assert row.kind is LedgerKind.RESERVATION
    assert row.amount == Decimal("2.00")
    assert ledger.outstanding_reservations() == Decimal("2.00")


def test_reverse_negates_the_outstanding_reservation(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    row = ledger.reverse(gen_id)

    assert row is not None
    assert row.amount == Decimal("-2.00")
    assert ledger.outstanding_reservations() == Decimal("0")


def test_reverse_twice_returns_none(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.reverse(gen_id)

    assert ledger.reverse(gen_id) is None


def test_reverse_with_no_reservation_returns_none(ledger, gen_id):
    assert ledger.reverse(gen_id) is None


def test_record_actual_reverses_then_records(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    row = ledger.record_actual(gen_id, Decimal("0.25"))

    assert row.kind is LedgerKind.ACTUAL
    assert row.cost_known is True
    kinds = [r.kind for r in ledger.db.list_ledger_for_generation(gen_id)]
    assert kinds == [LedgerKind.RESERVATION, LedgerKind.REVERSAL, LedgerKind.ACTUAL]


def test_a_completed_job_nets_to_its_actual_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))

    assert ledger.spend_for_day().total == Decimal("0.25")


def test_a_null_cost_leaves_the_reservation_standing(ledger, gen_id):
    # Spec section 3.4: the reservation stands as the recorded charge.
    ledger.reserve(gen_id, Decimal("2.00"))

    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().total == Decimal("2.00")
    assert ledger.outstanding_reservations() == Decimal("2.00")


def test_a_null_cost_marks_the_day_as_a_lower_bound(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().is_lower_bound is True


def test_zero_is_never_recorded_as_the_charge(ledger, gen_id):
    # Recording zero would let the cap silently never trip.
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().total != Decimal("0")


def test_settle_failed_nets_to_zero(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    ledger.settle_failed(gen_id)

    assert ledger.spend_for_day().total == Decimal("0")
    assert ledger.spend_for_day().is_lower_bound is False


def test_spend_for_day_sums_signed_amounts(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))
    ledger.reserve(gen_id, Decimal("1.00"))

    assert ledger.spend_for_day().total == Decimal("1.25")


def test_spend_for_day_excludes_other_days(ledger, gen_id, monkeypatch):
    freeze(monkeypatch, "2026-07-17T23:59:59.999999+00:00")
    ledger.reserve(gen_id, Decimal("5.00"))
    freeze(monkeypatch, "2026-07-18T09:00:00.000000+00:00")
    ledger.reserve(gen_id, Decimal("1.00"))

    assert ledger.spend_for_day(date(2026, 7, 18)).total == Decimal("1.00")
    assert ledger.spend_for_day(date(2026, 7, 17)).total == Decimal("5.00")


def test_spend_for_day_reports_outstanding_reservations(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    assert ledger.spend_for_day().reserved == Decimal("2.00")

    ledger.reverse(gen_id)

    assert ledger.spend_for_day().reserved == Decimal("0")


def test_outstanding_reservations_across_all_time(ledger, db, gen_id, monkeypatch):
    freeze(monkeypatch, "2026-07-01T00:00:00.000000+00:00")
    ledger.reserve(gen_id, Decimal("2.00"))
    freeze(monkeypatch, "2026-07-18T00:00:00.000000+00:00")

    # The cap window does not reset on restart, and nor does this figure.
    assert ledger.outstanding_reservations() == Decimal("2.00")
    assert ledger.spend_for_day(date(2026, 7, 18)).total == Decimal("0")


def test_generation_total_reports_known_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))

    assert ledger.generation_total(gen_id) == (Decimal("0.25"), True)


def test_generation_total_reports_unknown_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.generation_total(gen_id) == (Decimal("2.00"), False)


def test_generation_total_of_an_unrecorded_generation(ledger, gen_id):
    # No actual row means the cost is unknown, not zero.
    assert ledger.generation_total(gen_id) == (Decimal("0"), False)


def test_utc_day_bounds_is_half_open():
    start, end = utc_day_bounds(date(2026, 7, 18))

    assert start == "2026-07-18T00:00:00+00:00"
    assert end == "2026-07-19T00:00:00+00:00"


def test_utc_day_bounds_string_compare_includes_midnight():
    # Timestamps carry microseconds ('.') where the bounds carry an offset
    # ('+'), and '.' sorts after '+', so a midnight event lands inside its own
    # day and outside the next one under plain string comparison.
    start, end = utc_day_bounds(date(2026, 7, 18))
    midnight = "2026-07-18T00:00:00.000000+00:00"
    next_midnight = "2026-07-19T00:00:00.000000+00:00"

    assert start <= midnight < end
    assert not (start <= next_midnight < end)


def test_ledger_is_append_only_across_a_reopen(tmp_path, monkeypatch):
    path = tmp_path / "state" / "higgshole.db"
    with Database(path) as first:
        first.migrate()
        project = first.get_project_by_slug("unsorted")
        generation = first.create_generation(
            project_id=project.id,
            kind=GenerationKind.IMAGE,
            model="a/b",
            prompt="p",
            params={},
        )
        Ledger(first).reserve(generation.id, Decimal("2.00"))
        Ledger(first).record_actual(generation.id, Decimal("0.25"))

    with Database(path) as second:
        rows = second.list_ledger_for_generation(generation.id)

    assert [r.kind for r in rows] == [
        LedgerKind.RESERVATION,
        LedgerKind.REVERSAL,
        LedgerKind.ACTUAL,
    ]
    assert Ledger(Database(path)).generation_total(generation.id) == (
        Decimal("0.25"),
        True,
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/budget/test_ledger.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.budget.ledger'`

- [ ] **Step 3: Implement**

Create `src/higgshole/budget/ledger.py`:

```python
"""The local spend record: append-only, signed amounts, summed in Python.

Every terminal state appends a reversal, so spend for a window is the plain
sum of `amount` and can never be double-counted (spec 3.3). The one deliberate
exception is a completed job that reports no cost: its reservation stands as
the recorded charge and the day is marked as a lower bound, because recording
zero would let the cap silently never trip (spec 3.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from higgshole.store.db import Database, LedgerKind, LedgerRow

ZERO = Decimal("0")


@dataclass(frozen=True)
class DaySpend:
    """Spend across one UTC calendar day."""

    day: date
    total: Decimal
    is_lower_bound: bool
    reserved: Decimal


@dataclass(frozen=True)
class BudgetStatus:
    """What get_budget returns (spec 3.2).

    Provider figures are authoritative; ledger figures govern only the local
    cap.
    """

    provider_limit: Decimal | None
    provider_remaining: Decimal | None
    provider_usage_daily: Decimal | None
    provider_available: bool
    cap: Decimal | None
    spent_today: Decimal
    remaining_today: Decimal | None
    is_lower_bound: bool
    in_flight: int
    max_in_flight: int


def utc_day_bounds(day: date) -> tuple[str, str]:
    """[start, end) ISO-8601 UTC strings bounding one calendar day.

    Comparison against recorded_at is a plain string comparison under SQLite's
    BINARY collation. That is exact here because every stored timestamp shares
    the same fixed-width prefix, and because '.' (microseconds) sorts after
    '+' (the offset), so a timestamp at exactly midnight falls inside its own
    day and outside the following one.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


class Ledger:
    """Append-only signed-amount ledger.

    Every method is a pure function of the rows in spend_ledger plus the
    clock; no in-memory state survives a restart, which is what allows the cap
    window to keep counting across one.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        """The underlying database. Exposed for callers that must read raw
        ledger rows, such as reservation recovery at boot."""
        return self._db

    def _outstanding_for(self, generation_id: str) -> Decimal:
        """Reservations for one generation, net of any reversals."""
        return sum(
            (
                row.amount
                for row in self._db.list_ledger_for_generation(generation_id)
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )

    def reserve(self, generation_id: str, amount: Decimal) -> LedgerRow:
        """Append a positive `reservation`. Called only inside the gate's lock.

        cost_known is False: a reservation is a ceiling, not an observation.
        """
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.RESERVATION,
            amount=amount,
            cost_known=False,
        )

    def reverse(self, generation_id: str) -> LedgerRow | None:
        """Append a `reversal` negating the outstanding reservation.

        Returns None when there is nothing to reverse (already reversed, or
        none was taken). Called on EVERY terminal state.
        """
        outstanding = self._outstanding_for(generation_id)
        if outstanding <= ZERO:
            return None
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.REVERSAL,
            amount=-outstanding,
            cost_known=False,
        )

    def record_actual(self, generation_id: str, cost: Decimal | None) -> LedgerRow:
        """Record the provider-reported cost.

        cost is not None -> reverse() then append `actual` with cost_known=1.
        cost is None     -> the reservation STANDS as the recorded charge; an
                            `actual` row of amount 0 with cost_known=0 is
                            appended to mark the day as a lower bound. The
                            reservation is deliberately NOT reversed, and zero
                            is never recorded as the charge (spec 3.4).
        """
        if cost is None:
            return self._db.append_ledger(
                generation_id=generation_id,
                kind=LedgerKind.ACTUAL,
                amount=ZERO,
                cost_known=False,
            )

        self.reverse(generation_id)
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.ACTUAL,
            amount=cost,
            cost_known=True,
        )

    def settle_failed(self, generation_id: str) -> None:
        """Reverse the reservation with no actual, so a failed job nets to
        zero (spec 3.3)."""
        self.reverse(generation_id)

    def spend_for_day(self, day: date | None = None) -> DaySpend:
        """Sum signed amounts over one UTC calendar day of recorded_at.

        Defaults to today. The cap window does not reset on restart.
        """
        target = day or datetime.now(UTC).date()
        start, end = utc_day_bounds(target)
        rows = self._db.list_ledger_between(start_iso=start, end_iso=end)

        total = sum((row.amount for row in rows), ZERO)
        reserved = sum(
            (
                row.amount
                for row in rows
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )
        lower_bound = any(
            row.kind is LedgerKind.ACTUAL and not row.cost_known for row in rows
        )

        return DaySpend(
            day=target,
            total=total,
            is_lower_bound=lower_bound,
            reserved=reserved,
        )

    def outstanding_reservations(self) -> Decimal:
        """Sum of reservations with no matching reversal, across all time."""
        rows = self._db.list_ledger_between(start_iso="", end_iso="~")
        return sum(
            (
                row.amount
                for row in rows
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )

    def generation_total(self, generation_id: str) -> tuple[Decimal, bool]:
        """(net amount, cost_known) for one generation.

        cost_known is False when no `actual` row exists at all — a rejected or
        failed generation has no cost, which is not the same as costing zero,
        and the caller renders it as None rather than as a price.
        """
        rows = self._db.list_ledger_for_generation(generation_id)
        total = sum((row.amount for row in rows), ZERO)
        actuals = [row for row in rows if row.kind is LedgerKind.ACTUAL]
        known = bool(actuals) and all(row.cost_known for row in actuals)
        return total, known
```

> `outstanding_reservations` bounds the scan with `""` and `"~"` rather than adding a new query: every stored timestamp begins with a digit, and `'~'` (0x7E) sorts after every digit and every letter, so the range covers all rows while reusing the one indexed lookup the contract defines.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/budget/test_ledger.py -v`

Expected: PASS — `20 passed`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/budget/ledger.py tests/budget/test_ledger.py
git commit -m "feat: add the append-only spend ledger with a UTC-day cap window"
```

---

## Task 13: The serialized reservation gate

**Files:**
- Create: `src/higgshole/budget/gate.py`
- Modify: `src/higgshole/budget/__init__.py`
- Test: `tests/budget/test_gate.py`

**Interfaces:**
- Consumes: `store.db.Database`, `TERMINAL_STATES`, `budget.ledger.Ledger`, `budget.ledger.BudgetStatus`, `budget.estimator.Estimate`, `budget.estimator.reservation_amount`, `orclient.types.KeyStatus`, `config.Settings`.
- Produces: `Reservation`, `GateDecision`, `GateRejection`, `BudgetGate`.

- [ ] **Step 1: Write the failing test**

Create `tests/budget/test_gate.py`:

```python
import asyncio
from decimal import Decimal

import pytest

from higgshole.budget.estimator import Estimate, EstimateUnavailable
from higgshole.budget.gate import BudgetGate, GateDecision, GateRejection, Reservation
from higgshole.budget.ledger import Ledger
from higgshole.config import Settings
from higgshole.orclient.types import KeyStatus
from higgshole.store.db import Database, GenerationKind, GenerationState

EXACT = Estimate(amount=Decimal("0.28"), reason=None, detail="5s at 0.056/s")
UNKNOWN = Estimate(
    amount=None,
    reason=EstimateUnavailable.VIDEO_TOKEN_PRICED,
    detail="priced per video token",
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def ledger(db):
    return Ledger(db)


def new_generation(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    ).id


def make_gate(db, ledger, *, cap="10.00", ceiling="2.00", in_flight=3):
    return BudgetGate(
        db,
        ledger,
        daily_cap_usd=None if cap is None else Decimal(cap),
        max_job_cost_usd=Decimal(ceiling),
        max_in_flight=in_flight,
    )


async def test_an_exact_estimate_reserves_the_estimate(db, ledger):
    gate = make_gate(db, ledger)
    gen_id = new_generation(db)

    granted = await gate.acquire(generation_id=gen_id, estimate=EXACT)

    assert isinstance(granted, Reservation)
    assert granted.amount == Decimal("0.28")
    assert granted.from_exact_estimate is True
    assert granted.ledger_row_id > 0
    assert ledger.spend_for_day().total == Decimal("0.28")


async def test_a_non_estimable_job_reserves_the_ceiling(db, ledger):
    # Spec section 3.3: the pessimistic ceiling stands in for an estimate.
    gate = make_gate(db, ledger, ceiling="2.00")
    gen_id = new_generation(db)

    granted = await gate.acquire(generation_id=gen_id, estimate=UNKNOWN)

    assert granted.amount == Decimal("2.00")
    assert granted.from_exact_estimate is False


async def test_the_cap_rejects_a_job_that_would_exceed_it(db, ledger):
    gate = make_gate(db, ledger, cap="3.00", ceiling="2.00", in_flight=10)
    await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    assert isinstance(rejection, GateRejection)
    assert rejection.decision is GateDecision.CAP_EXCEEDED


async def test_a_rejection_reports_the_remaining_balance(db, ledger):
    gate = make_gate(db, ledger, cap="3.00", ceiling="2.00", in_flight=10)
    await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    assert rejection.cap == Decimal("3.00")
    assert rejection.spent_today == Decimal("2.00")
    assert rejection.remaining_today == Decimal("1.00")
    assert rejection.would_reserve == Decimal("2.00")
    assert "cap" in rejection.message.lower()


async def test_no_cap_means_no_cap_rejection(db, ledger):
    gate = make_gate(db, ledger, cap=None, ceiling="2.00", in_flight=10)

    for _ in range(5):
        granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)
        assert isinstance(granted, Reservation)


async def test_the_in_flight_ceiling_rejects(db, ledger):
    gate = make_gate(db, ledger, in_flight=1)
    new_generation(db)  # a second row occupying a non-terminal state

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=EXACT)

    assert rejection.decision is GateDecision.IN_FLIGHT_LIMIT


async def test_the_generation_being_gated_does_not_count_against_itself(db, ledger):
    gate = make_gate(db, ledger, in_flight=1)

    granted = await gate.acquire(generation_id=new_generation(db), estimate=EXACT)

    assert isinstance(granted, Reservation)


async def test_concurrent_acquisitions_cannot_exceed_the_cap(db, ledger):
    # Spec section 3.3: without the lock, ten submissions in one second would
    # each observe the same remaining balance.
    gate = make_gate(db, ledger, cap="5.00", ceiling="2.00", in_flight=100)
    ids = [new_generation(db) for _ in range(10)]

    results = await asyncio.gather(
        *(gate.acquire(generation_id=gen_id, estimate=UNKNOWN) for gen_id in ids)
    )

    granted = [r for r in results if isinstance(r, Reservation)]
    assert len(granted) == 2
    assert ledger.spend_for_day().total == Decimal("4.00")
    assert ledger.spend_for_day().total <= Decimal("5.00")


async def test_release_on_success_records_the_actual_cost(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=Decimal("0.25"), succeeded=True)

    assert ledger.generation_total(granted.generation_id) == (Decimal("0.25"), True)


async def test_release_on_success_with_no_cost_leaves_the_reservation(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=None, succeeded=True)

    assert ledger.generation_total(granted.generation_id) == (Decimal("2.00"), False)
    assert ledger.spend_for_day().is_lower_bound is True


async def test_release_on_failure_nets_to_zero(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=None, succeeded=False)

    assert ledger.spend_for_day().total == Decimal("0")


def test_cap_is_set_reflects_configuration(db, ledger):
    # Spec section 3.5: quality=auto is refused whenever a cap exists, at any
    # remaining balance.
    assert make_gate(db, ledger, cap="1.00").cap_is_set is True
    assert make_gate(db, ledger, cap=None).cap_is_set is False
    assert make_gate(db, ledger, cap="1.00").cap == Decimal("1.00")


async def test_status_uses_provider_figures_when_available(db, ledger):
    gate = make_gate(db, ledger, cap="10.00")
    await gate.acquire(generation_id=new_generation(db), estimate=EXACT)
    key_status = KeyStatus.from_api(
        {"data": {"limit": 100, "limit_remaining": 74.5, "usage_daily": 25.5}}
    )

    status = await gate.status(key_status)

    assert status.provider_available is True
    assert status.provider_remaining == Decimal("74.5")
    assert status.provider_usage_daily == Decimal("25.5")
    assert status.spent_today == Decimal("0.28")
    assert status.remaining_today == Decimal("9.72")
    assert status.max_in_flight == 3


async def test_status_marks_provider_unavailable_when_the_key_call_failed(db, ledger):
    # Spec section 3.2: the UI then labels the figures local-only.
    gate = make_gate(db, ledger)

    status = await gate.status(None)

    assert status.provider_available is False
    assert status.provider_limit is None
    assert status.provider_remaining is None


async def test_status_reports_a_lower_bound_day(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)
    await gate.release(granted, actual_cost=None, succeeded=True)

    status = await gate.status(None)

    assert status.is_lower_bound is True
    assert status.spent_today == Decimal("2.00")


def test_from_settings_reads_the_cap_and_ceilings(db, ledger, monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_DAILY_CAP_USD", "7.50")
    monkeypatch.setenv("HIGGSHOLE_MAX_JOB_COST_USD", "3.00")
    monkeypatch.setenv("HIGGSHOLE_MAX_IN_FLIGHT", "5")

    gate = BudgetGate.from_settings(db, ledger, Settings())

    assert gate.cap == Decimal("7.50")
    assert gate._max_job_cost_usd == Decimal("3.00")
    assert gate._max_in_flight == 5


async def test_a_rejection_is_returned_not_raised(db, ledger):
    # Rejection is a normal budget outcome, not an error condition.
    gate = make_gate(db, ledger, cap="0.10", ceiling="2.00", in_flight=10)
    gen_id = new_generation(db)

    result = await gate.acquire(generation_id=gen_id, estimate=UNKNOWN)

    assert isinstance(result, GateRejection)
    assert db.get_generation(gen_id).state is GenerationState.PENDING
    assert ledger.spend_for_day().total == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/budget/test_gate.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'higgshole.budget.gate'`

- [ ] **Step 3: Implement**

Create `src/higgshole/budget/gate.py`:

```python
"""The serialized reservation gate (spec 3.3).

Estimate, cap check and reservation write happen inside ONE process-wide async
lock, so ten submissions in one second cannot each observe the same remaining
balance. The lock is process-local, which is why the deployment runs exactly
one uvicorn worker (spec 9).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from higgshole.config import Settings
from higgshole.orclient.types import KeyStatus
from higgshole.store.db import TERMINAL_STATES, Database

from .estimator import Estimate, reservation_amount
from .ledger import BudgetStatus, Ledger

ZERO = Decimal("0")


@dataclass(frozen=True)
class Reservation:
    """A granted reservation. Held by the job runner until a terminal state."""

    generation_id: str
    amount: Decimal
    from_exact_estimate: bool
    ledger_row_id: int


class GateDecision(StrEnum):
    ALLOWED = "allowed"
    CAP_EXCEEDED = "cap_exceeded"
    IN_FLIGHT_LIMIT = "in_flight_limit"


@dataclass(frozen=True)
class GateRejection:
    decision: GateDecision
    message: str
    cap: Decimal | None
    spent_today: Decimal
    remaining_today: Decimal | None
    would_reserve: Decimal


class BudgetGate:
    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        *,
        daily_cap_usd: Decimal | None,
        max_job_cost_usd: Decimal,
        max_in_flight: int,
    ) -> None:
        self._db = db
        self._ledger = ledger
        self._cap = daily_cap_usd
        self._max_job_cost_usd = max_job_cost_usd
        self._max_in_flight = max_in_flight
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls, db: Database, ledger: Ledger, settings: Settings
    ) -> BudgetGate:
        return cls(
            db,
            ledger,
            daily_cap_usd=settings.daily_cap_usd,
            max_job_cost_usd=settings.max_job_cost_usd,
            max_in_flight=settings.max_in_flight,
        )

    @property
    def cap(self) -> Decimal | None:
        return self._cap

    @property
    def cap_is_set(self) -> bool:
        """Passed to validate_image_request(daily_cap_set=...).

        quality=auto is refused whenever a cap exists, at any remaining
        balance, because it is unbounded by definition (spec 3.5).
        """
        return self._cap is not None

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

    async def acquire(
        self, *, generation_id: str, estimate: Estimate
    ) -> Reservation | GateRejection:
        """Serialized: count in-flight, compute today's spend, reserve.

        Returns a Reservation on success, or a GateRejection which the caller
        turns into state REJECTED with the matching ErrorReason. Never raises
        for a budget outcome — rejection is a normal result, not an error.
        """
        async with self._lock:
            amount, exact = reservation_amount(
                estimate, max_job_cost_usd=self._max_job_cost_usd
            )
            day = self._ledger.spend_for_day()
            remaining = None if self._cap is None else self._cap - day.total

            in_flight = self._in_flight_excluding(generation_id)
            if in_flight >= self._max_in_flight:
                return GateRejection(
                    decision=GateDecision.IN_FLIGHT_LIMIT,
                    message=(
                        f"{in_flight} generation(s) already in flight; the ceiling "
                        f"is {self._max_in_flight}. Try again when one finishes."
                    ),
                    cap=self._cap,
                    spent_today=day.total,
                    remaining_today=remaining,
                    would_reserve=amount,
                )

            if self._cap is not None and day.total + amount > self._cap:
                return GateRejection(
                    decision=GateDecision.CAP_EXCEEDED,
                    message=(
                        f"the local daily cap of {self._cap} USD would be exceeded: "
                        f"{day.total} already recorded today and this job reserves "
                        f"{amount}."
                    ),
                    cap=self._cap,
                    spent_today=day.total,
                    remaining_today=remaining,
                    would_reserve=amount,
                )

            row = self._ledger.reserve(generation_id, amount)
            return Reservation(
                generation_id=generation_id,
                amount=amount,
                from_exact_estimate=exact,
                ledger_row_id=row.id,
            )

    async def release(
        self,
        reservation: Reservation,
        *,
        actual_cost: Decimal | None,
        succeeded: bool,
    ) -> None:
        """Settle on any terminal state.

        succeeded=False           -> ledger.settle_failed (nets to zero)
        succeeded, cost is None   -> ledger.record_actual(None): reservation
                                     stands, day marked as a lower bound
        succeeded, cost present   -> ledger.record_actual(cost)

        Held under the same lock as acquire so that a settlement cannot land
        between another submission's spend read and its reservation write.
        """
        async with self._lock:
            if not succeeded:
                self._ledger.settle_failed(reservation.generation_id)
                return
            self._ledger.record_actual(reservation.generation_id, actual_cost)

    async def status(self, key_status: KeyStatus | None) -> BudgetStatus:
        """Assemble BudgetStatus.

        `key_status=None` means the free GET /api/v1/key call failed, so
        provider_available is False and the UI labels the figures local-only
        (spec 3.2).
        """
        day = self._ledger.spend_for_day()
        return BudgetStatus(
            provider_limit=key_status.limit if key_status else None,
            provider_remaining=key_status.limit_remaining if key_status else None,
            provider_usage_daily=key_status.usage_daily if key_status else None,
            provider_available=key_status is not None,
            cap=self._cap,
            spent_today=day.total,
            remaining_today=None if self._cap is None else self._cap - day.total,
            is_lower_bound=day.is_lower_bound,
            in_flight=self._db.count_in_flight(),
            max_in_flight=self._max_in_flight,
        )
```

Replace `src/higgshole/budget/__init__.py`:

```python
"""Cost estimation, the spend ledger, and the reservation gate."""

from .estimator import (
    CENTS_PREFIX,
    TOKEN_UNITS,
    Estimate,
    EstimateUnavailable,
    estimate_image_cost,
    estimate_video_cost,
    parse_sku_amount,
    reservation_amount,
)
from .gate import BudgetGate, GateDecision, GateRejection, Reservation
from .ledger import BudgetStatus, DaySpend, Ledger, utc_day_bounds

__all__ = [
    "CENTS_PREFIX",
    "TOKEN_UNITS",
    "BudgetGate",
    "BudgetStatus",
    "DaySpend",
    "Estimate",
    "EstimateUnavailable",
    "GateDecision",
    "GateRejection",
    "Ledger",
    "Reservation",
    "estimate_image_cost",
    "estimate_video_cost",
    "parse_sku_amount",
    "reservation_amount",
    "utc_day_bounds",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q && uv run ruff check .`

Expected: PASS — the whole suite, including `tests/budget/test_gate.py`'s `17 passed`, and `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/higgshole/budget/gate.py src/higgshole/budget/__init__.py tests/budget/test_gate.py
git commit -m "feat: add the serialized reservation gate and in-flight ceiling"
```

---

## Definition of done

- [ ] `uv run pytest -q` passes with no network access and no billable call
- [ ] `uv run ruff check .` is clean
- [ ] 234 new test functions across 13 tasks, all green (up to 4 skipped where `ffmpeg`/`ffprobe` are absent)
- [ ] `store/` imports nothing from `higgshole.orclient` — proven in a fresh interpreter by `tests/store/test_package.py`
- [ ] All eight tables from spec §5.4 exist, with every index and constraint in the frozen DDL
- [ ] `spend_ledger` is written only through `Database.append_ledger`; no `UPDATE` or `DELETE` statement targets it
- [ ] `spend_ledger.amount` is stored as `TEXT` and every window total is summed in Python
- [ ] Every monetary value crossing a module boundary is `Decimal | None`; no `float`, and no `0` standing in for unknown
- [ ] The estimator returns `None` with a machine-readable reason for: token-priced images, `video_tokens`-priced video, the Kling audio/resolution axis collision, an audio-capable model with only a bare SKU, an unknown unit, and an empty pricing cache
- [ ] `cents_per_*` SKUs are divided by 100 — a $0.35 job is never quoted as $35
- [ ] A completed job reporting no cost leaves its reservation standing and marks the day as a lower bound
- [ ] A failed job nets to exactly zero
- [ ] Concurrent submissions cannot exceed the daily cap (proved by `test_concurrent_acquisitions_cannot_exceed_the_cap`)
- [ ] Path traversal is rejected for `..`, nested `..`, and absolute paths
- [ ] Interrupted writes leave only a `.part` file, which is never promoted
- [ ] No media fixture is committed; ffmpeg tests generate their own and skip when the binaries are absent
- [ ] No committed file contains a personal name, an employer name, a machine-specific path, or an API key

---

## Contract additions

The frozen contract did not specify the following. Each is added in the most
consistent style available and is required by a component the contract does
name. Plans 3–5 may rely on them.

| Symbol | Module | Why it was needed |
|---|---|---|
| `probe_video_streams(path) -> dict` | `store/metadata.py` | The plan scope requires ffprobe to report **fps, codec and has_audio**, but `MediaMetadata` is frozen at six fields with no column for them in `assets`. A separate accessor keeps the frozen type intact while making the data available to the detail view. |
| `read_embedded_params_from_image(image) -> dict` | `store/metadata.py` | `probe_image` already holds an open Pillow handle; reopening the file inside `read_embedded_params` would double the I/O for every probe. The path-taking `read_embedded_params` from the contract is unchanged and delegates to it. |
| `_run(args, *, timeout)` | `store/metadata.py` | The contract requires "every subprocess call goes through one helper so tests stub a single seam" but does not name it. Private by intent; only tests reference it. |
| `Database.__init__(..., *, _uri=None)` | `store/db.py` | `Database.in_memory()` is in the contract but has no way to reach `sqlite3.connect(uri=True)` through the public signature. Private, keyword-only, defaulted — the public `__init__(path)` signature is unchanged. |
| `LAST_ERROR_KEY` | `catalog/cache.py` | The settings key `catalog_last_refresh_error` is frozen in the contract's settings table; naming the constant prevents the string being typed twice. |
| `video_capabilities(model)` / `image_capabilities(model)` | `catalog/cache.py` | `CatalogCache` receives parsed `VideoModel`/`ImageModel` objects from `orclient` but must persist a `capabilities` JSON blob that `from_api` can re-parse. These serialise back to the provider's payload shape so `from_api` remains the single parser. |
| `CatalogCache.ttl_hours` / `CatalogCache.client_factory` | `catalog/cache.py` | Read-only properties so `from_settings` is testable without reaching into private attributes. |
| `Ledger._outstanding_for(generation_id)` | `budget/ledger.py` | Private helper backing `reverse` and `record_actual`; the contract describes the behaviour but names no accessor. |
| `ZERO` | `budget/ledger.py`, `budget/gate.py` | A module-level `Decimal("0")` so that `sum(..., ZERO)` never starts from an `int` and never introduces a `float`. |
| `MediaPaths.allocate_output(kind: str)` | `store/paths.py` | Annotated `str` rather than `GenerationKind` because `paths.py` must not import `db.py` (which imports `paths.py`). `GenerationKind` is a `StrEnum`, so callers pass the enum member unchanged and the contract's call sites are unaffected. |
