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
