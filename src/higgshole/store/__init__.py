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
