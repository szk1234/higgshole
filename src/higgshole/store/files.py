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
