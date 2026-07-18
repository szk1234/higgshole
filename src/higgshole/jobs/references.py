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
