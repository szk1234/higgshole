"""The guarantee behind spec section 6.3.

GZipMiddleware compresses partial content while Content-Range continues to
describe the uncompressed entity, and it does not exempt video/mp4 — so a
browser seeking through a video gets corrupt bytes. HiggsHole never registers
it, but "we promise not to" is not a mechanism. These tests add the middleware
deliberately and prove media is still served untouched.
"""

import pytest
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tests.web.fakes import build_test_state

PAYLOAD = bytes(range(256)) * 40  # 10240 highly compressible bytes


@pytest.fixture
def gzipped_client(db, media_paths):
    from higgshole.web.app import create_app

    video = media_paths.videos_dir("unsorted") / "clip.mp4"
    video.write_bytes(PAYLOAD)

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    # Deliberately hostile: the exact middleware the spec forbids.
    app.add_middleware(GZipMiddleware, minimum_size=1)

    @app.get("/_bulk")
    async def _bulk() -> JSONResponse:
        return JSONResponse({"filler": "x" * 4096})

    with TestClient(app) as client:
        yield client


def test_the_middleware_really_is_active(gzipped_client):
    # Without this the other three assertions could pass vacuously.
    response = gzipped_client.get("/_bulk", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"


def test_a_full_media_response_is_never_compressed(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.content == PAYLOAD


def test_a_206_response_carries_no_content_encoding(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=100-599"},
    )

    assert response.status_code == 206
    assert "content-encoding" not in response.headers


def test_a_206_content_range_still_describes_the_bytes_returned(gzipped_client):
    response = gzipped_client.get(
        "/media/projects/unsorted/videos/clip.mp4",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=100-599"},
    )

    assert response.headers["content-range"] == f"bytes 100-599/{len(PAYLOAD)}"
    assert response.headers["content-length"] == "500"
    assert response.content == PAYLOAD[100:600]
