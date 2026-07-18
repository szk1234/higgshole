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
from collections.abc import Iterable, Iterator
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

        raise IdCollisionError(f"{ID_COLLISION_RETRIES} fresh identifiers all collided")

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
            clauses.append("g.project_id IN (SELECT id FROM projects WHERE slug = ?)")
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
