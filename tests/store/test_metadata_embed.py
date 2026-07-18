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
