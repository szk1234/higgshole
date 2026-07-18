"""What a media file itself reports, and what we write back into it.

Two external tools are involved: Pillow for stills and ffmpeg/ffprobe for
video. Every subprocess call goes through the single ``_run`` seam so that
tests stub one function rather than the whole of ``subprocess``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, PngImagePlugin

from .files import atomic_write_bytes, delete_quietly, discard_part, file_size

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
    """Extract one frame to `destination` as WebP.

    ffmpeg decodes the frame to PNG and Pillow performs the WebP encode. Going
    straight to ``-f webp`` would bind us to an ffmpeg built with libwebp,
    which many distribution builds omit — the muxer is always present but the
    encoder frequently is not, and that failure only surfaces at the first
    video generation. Pillow's WebP support is a hard dependency here, so the
    encode is guaranteed to be available wherever the application runs.
    """
    part = destination.with_name(destination.name + ".part")
    frame = destination.with_name(destination.name + ".frame.png")
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
                "image2",
                "-c:v",
                "png",
                "-y",
                str(frame),
            ]
        )

        # Seeking past the end of a short clip makes ffmpeg exit 0 having
        # written nothing. Report that as a MetadataError so make_video_poster
        # falls back to timestamp 0 rather than raising an opaque I/O error.
        if not frame.exists() or file_size(frame) == 0:
            raise MetadataError(
                f"ffmpeg produced no frame from {source} at {at_seconds:.3f}s"
            )

        buffer = io.BytesIO()
        with Image.open(frame) as image:
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.save(buffer, format=THUMBNAIL_FORMAT.upper(), quality=82, method=4)
        part.write_bytes(buffer.getvalue())
    except BaseException:
        discard_part(destination)
        raise
    finally:
        delete_quietly(frame)

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
