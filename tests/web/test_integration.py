import asyncio
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from higgshole.store.db import AssetKind, GenerationKind, GenerationState
from tests.web.fakes import build_test_state

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def app_client(db, media_paths):
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    with TestClient(app) as client:
        yield client, state


def test_the_media_mount_and_the_api_share_one_application(app_client):
    client, state = app_client
    relative = "projects/unsorted/images/probe.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)

    assert client.get("/api/projects").status_code == 200
    assert client.get(f"/media/{relative}").status_code == 200
    assert client.get("/").status_code == 200


def test_a_completed_generation_is_browsable_and_playable(app_client):
    client, state = app_client
    project = state.db.get_project_by_slug("unsorted")
    row = state.db.create_generation(
        project_id=project.id,
        kind=GenerationKind.IMAGE,
        model="openai/gpt-image-2",
        prompt="end to end",
        params={},
        state=GenerationState.COMPLETE,
    )
    relative = f"projects/unsorted/images/{row.id}.png"
    target = state.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    state.db.set_generation_file(row.id, relative)
    state.db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path=relative,
        mime_type="image/png",
        bytes_=len(PNG_1X1),
        generation_id=row.id,
        width=1,
        height=1,
    )

    listed = client.get("/api/media").json()
    url = listed["items"][0]["asset"]["url"]

    assert client.get(url).content == PNG_1X1
    assert row.id in client.get("/library").text


async def test_the_event_stream_advertises_the_sse_content_type(db, media_paths):
    # Driven as raw ASGI rather than through TestClient: an event stream never
    # completes, and TestClient runs a response to completion before returning
    # it, so any client-side fetch of this route would block forever.
    from higgshole.web.app import create_app

    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events/jobs",
        "raw_path": b"/events/jobs",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    started: dict = {}
    start_seen = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()  # the client never disconnects

    async def send(message):
        if message["type"] == "http.response.start":
            started.update(message)
            start_seen.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(start_seen.wait(), timeout=5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert started["status"] == 200
    headers = {key.lower(): value for key, value in started["headers"]}
    assert headers[b"content-type"].startswith(b"text/event-stream")


def test_every_documented_route_is_present_in_the_schema(app_client):
    client, _ = app_client

    paths = set(client.get("/api/openapi.json").json()["paths"])

    assert {
        "/api/models",
        "/api/estimate",
        "/api/generate/image",
        "/api/generate/video",
        "/api/jobs",
        "/api/jobs/{gen_id}",
        "/api/uploads",
        "/api/media",
        "/api/media/{gen_id}",
        "/api/projects",
        "/api/budget",
        "/api/settings",
        "/api/settings/catalog/refresh",
        "/api/settings/rescan",
    } <= paths


def test_the_console_script_starts_the_single_worker_entrypoint():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["scripts"]["higgshole"] == "higgshole.web.app:run"


def test_no_source_file_references_an_external_asset_host():
    # The console must work on an offline LAN, so nothing may be fetched at
    # page load from anywhere but this service.
    offenders = []
    for path in Path("src/higgshole").rglob("*"):
        if path.suffix not in {".html", ".py", ".css"} or "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "cdn." in text or "unpkg.com" in text or "jsdelivr" in text:
            offenders.append(str(path))

    assert offenders == []
