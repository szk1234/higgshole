import base64

import pytest

from higgshole.jobs.references import (
    ReferenceTooLargeError,
    ReferenceTransport,
    UnsupportedTransportError,
    build_input_references,
    build_reference,
    build_video_frame_images,
    encode_data_uri,
    video_references_supported,
)
from higgshole.store.db import AssetKind, AssetRow, InputRole, utc_now_iso
from higgshole.store.paths import MediaPaths, PathTraversalError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stub-pixels"


def _asset(relative_path: str, *, asset_id: str, mime_type: str = "image/png") -> AssetRow:
    return AssetRow(
        id=asset_id,
        generation_id=None,
        kind=AssetKind.UPLOAD,
        file_path=relative_path,
        mime_type=mime_type,
        bytes=len(PNG_BYTES),
        width=4,
        height=4,
        duration_s=None,
        created_at=utc_now_iso(),
    )


@pytest.fixture
def paths(tmp_path):
    media_paths = MediaPaths(tmp_path / "media")
    media_paths.ensure_project_tree("unsorted")
    return media_paths


def _write_upload(paths, name: str) -> str:
    target = paths.uploads_dir("unsorted") / name
    target.write_bytes(PNG_BYTES)
    return target.relative_to(paths.root).as_posix()


def test_encode_data_uri_produces_a_base64_payload(paths):
    relative = _write_upload(paths, "a.png")

    uri = encode_data_uri(paths.root / relative)

    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_BYTES


def test_encode_data_uri_rejects_an_oversized_file(paths):
    # A multi-megabyte data URI inflates the request body by roughly a third
    # and some providers reject it outright, so the ceiling is enforced here.
    relative = _write_upload(paths, "big.png")

    with pytest.raises(ReferenceTooLargeError):
        encode_data_uri(paths.root / relative, max_bytes=4)


def test_build_reference_resolves_an_asset_inside_the_media_root(paths):
    relative = _write_upload(paths, "ref.png")
    asset = _asset(relative, asset_id="0c118b4e77aa")

    uri = build_reference(asset, paths, transport=ReferenceTransport.DATA_URI)

    assert uri.startswith("data:image/png;base64,")


def test_build_reference_rejects_an_unknown_transport(paths):
    # A public-URL mode is explicitly out of scope (spec 2.8): making local
    # files provider-reachable needs a tunnel and contradicts the
    # trusted-network premise.
    relative = _write_upload(paths, "ref.png")
    asset = _asset(relative, asset_id="0c118b4e77aa")

    with pytest.raises(UnsupportedTransportError):
        build_reference(asset, paths, transport="public_url")


def test_build_video_frame_images_keeps_only_frame_roles(paths):
    first = _asset(_write_upload(paths, "first.png"), asset_id="aaaaaaaaaaaa")
    last = _asset(_write_upload(paths, "last.png"), asset_id="bbbbbbbbbbbb")
    other = _asset(_write_upload(paths, "other.png"), asset_id="cccccccccccc")

    frames = build_video_frame_images(
        [
            (first, InputRole.FIRST_FRAME),
            (other, InputRole.INPUT_REFERENCE),
            (last, InputRole.LAST_FRAME),
        ],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert [frame_type for _, frame_type in frames] == ["first_frame", "last_frame"]
    assert all(url.startswith("data:image/png;base64,") for url, _ in frames)


def test_build_input_references_keeps_only_reference_role(paths):
    reference = _asset(_write_upload(paths, "r.png"), asset_id="dddddddddddd")
    frame = _asset(_write_upload(paths, "f.png"), asset_id="eeeeeeeeeeee")

    urls = build_input_references(
        [(frame, InputRole.FIRST_FRAME), (reference, InputRole.INPUT_REFERENCE)],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert len(urls) == 1


def test_build_input_references_preserves_order(paths):
    one = _asset(_write_upload(paths, "1.png"), asset_id="111111111111")
    two = _asset(_write_upload(paths, "2.png"), asset_id="222222222222")
    (paths.root / two.file_path).write_bytes(PNG_BYTES + b"two")

    urls = build_input_references(
        [(one, InputRole.INPUT_REFERENCE), (two, InputRole.INPUT_REFERENCE)],
        paths,
        transport=ReferenceTransport.DATA_URI,
    )

    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG_BYTES
    assert base64.b64decode(urls[1].split(",", 1)[1]) == PNG_BYTES + b"two"


def test_video_references_supported_is_true_for_data_uri():
    # Open item 12.1 is unresolved: schema-level acceptance of data URIs by
    # video providers is near-certain but runtime acceptance is untested. If a
    # live test disproves it this returns False and the UI disables the slots.
    assert video_references_supported(ReferenceTransport.DATA_URI) is True


def test_reference_outside_the_media_root_is_refused(paths):
    escaped = _asset("../../etc/passwd", asset_id="ffffffffffff")

    with pytest.raises(PathTraversalError):
        build_reference(escaped, paths, transport=ReferenceTransport.DATA_URI)
