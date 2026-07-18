import pytest
from starlette.testclient import TestClient

from higgshole.web.media import (
    create_media_app,
    media_url_for,
    poster_url_for,
    thumb_url_for,
)

#: 2048 deterministic bytes, large enough for meaningful ranges.
PAYLOAD = bytes(range(256)) * 8


@pytest.fixture
def media_client(media_paths, db):
    video = media_paths.videos_dir("unsorted") / "clip.mp4"
    video.write_bytes(PAYLOAD)

    thumb = media_paths.thumbs_dir("unsorted") / "a3f21c9d4e07.webp"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"RIFF____WEBPVP8 ")

    with TestClient(create_media_app(media_paths, db)) as client:
        yield client


def test_a_full_request_returns_the_whole_file(media_client):
    response = media_client.get("/media/projects/unsorted/videos/clip.mp4")

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["accept-ranges"] == "bytes"


def test_a_range_request_returns_206_with_a_correct_content_range(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": "bytes=0-499"},
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-499/{len(PAYLOAD)}"
    assert response.headers["content-length"] == "500"
    assert response.content == PAYLOAD[:500]


def test_a_suffix_range_returns_the_final_bytes(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": "bytes=-500"},
    )

    assert response.status_code == 206
    assert response.content == PAYLOAD[-500:]
    start = len(PAYLOAD) - 500
    assert response.headers["content-range"] == (
        f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
    )


def test_an_unsatisfiable_range_returns_416(media_client):
    response = media_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Range": f"bytes={len(PAYLOAD) + 10}-{len(PAYLOAD) + 20}"},
    )

    assert response.status_code == 416


def test_a_missing_file_is_404(media_client):
    assert media_client.get("/media/projects/unsorted/videos/nope.mp4").status_code == 404


@pytest.mark.parametrize(
    "crafted",
    [
        "/media/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc/passwd",
        "/media/projects%2f..%2f..%2f..%2fetc/passwd",
    ],
)
def test_encoded_parent_traversal_is_rejected(media_client, crafted):
    response = media_client.get(crafted)

    # 404 rather than 403: a 403 would confirm that the target exists.
    assert response.status_code == 404
    assert b"root:" not in response.content


def test_an_absolute_path_cannot_escape_the_root(media_client):
    response = media_client.get("/media//etc/passwd")

    assert response.status_code == 404
    assert b"root:" not in response.content


def test_thumbnails_are_served_from_the_thumbs_tree(media_client):
    response = media_client.get("/thumbs/unsorted/a3f21c9d4e07.webp")

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")


def test_a_thumbnail_request_cannot_escape_its_project(media_client):
    response = media_client.get("/thumbs/unsorted/%2e%2e%2f%2e%2e%2fpasswd")

    assert response.status_code == 404


def test_url_helpers_are_the_single_place_urls_are_built():
    assert (
        media_url_for("projects/unsorted/images/20260718-143022_a3f21c9d4e07_x.png")
        == "/media/projects/unsorted/images/20260718-143022_a3f21c9d4e07_x.png"
    )
    assert (
        thumb_url_for(project_slug="unsorted", gen_id="a3f21c9d4e07")
        == "/thumbs/unsorted/a3f21c9d4e07.webp"
    )
    assert (
        poster_url_for(project_slug="unsorted", gen_id="a3f21c9d4e07")
        == "/thumbs/unsorted/a3f21c9d4e07_poster.webp"
    )
