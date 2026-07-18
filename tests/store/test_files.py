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
