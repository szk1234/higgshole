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
        directory = (
            self.images_dir(project_slug)
            if kind == "image"
            else self.videos_dir(project_slug)
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
