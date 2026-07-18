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

import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
