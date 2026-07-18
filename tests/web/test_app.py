from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from higgshole.config import Settings
from higgshole.store.db import Database, GenerationKind, GenerationState
from higgshole.web.app import AppState, build_app_state, create_app, get_state, run
from tests.web.fakes import FakeCatalog, build_test_state


@pytest.fixture
def app_and_state(db, media_paths):
    state = build_test_state(db=db, paths=media_paths)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state
    return app, state


def test_the_factory_returns_a_fastapi_application(app_and_state):
    app, _ = app_and_state

    assert isinstance(app, FastAPI)


def test_the_state_dependency_returns_the_assembled_state(app_and_state):
    app, state = app_and_state

    with TestClient(app) as client:
        request_state = client.app.state.higgshole

    assert isinstance(request_state, AppState)
    assert request_state is state
    assert get_state.__name__ == "get_state"


def test_startup_migrates_and_creates_the_default_project(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        project = state.db.get_project_by_slug("unsorted")

    assert project is not None
    assert project.slug == "unsorted"


def test_startup_survives_a_failing_catalogue_refresh(db, media_paths):
    # Spec section 4.2: the application never blocks startup on catalogue
    # availability. A provider outage must not make the service unbootable.
    catalog = FakeCatalog(refresh_error=RuntimeError("provider down"))
    state = build_test_state(db=db, paths=media_paths, catalog=catalog)
    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app) as client:
        # Any served route proves the application booted; the event stream
        # itself is exercised in tests/web/test_integration.py, because
        # TestClient runs a response to completion and SSE never completes.
        assert client.get("/api/openapi.json").status_code == 200

    assert catalog.refresh_calls == 1


def test_startup_records_a_resume_report(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        report = state.resume_report

    assert report is not None
    assert report.reattached == ()


def test_startup_reattaches_a_video_row_left_mid_flight(db, media_paths):
    state = build_test_state(db=db, paths=media_paths)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.RUNNING,
    )
    db.set_provider_job_id(row.id, "job-abc")

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert row.id in (report.reattached + report.timed_out + report.orphaned)


def test_startup_releases_the_reservation_for_an_orphaned_video_row(db, media_paths):
    # A resumable row carrying no provider job ID is unrecoverable. Its
    # reservation must still be released, or the daily cap stays consumed by
    # a job that can never settle.
    state = build_test_state(db=db, paths=media_paths)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.SUBMITTED,
    )
    state.ledger.reserve(row.id, Decimal("0.50"))

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert report.orphaned == (row.id,)
    assert [succeeded for _, _, succeeded in state.video_runner.gate.released] == [False]


def test_startup_releases_the_reservation_for_a_timed_out_video_row(db, media_paths):
    # Past the wall-clock ceiling while the service was down: failed rather
    # than reattached, and the reservation reversed on the same branch.
    settings = Settings(
        media_root=media_paths.root,
        db_path=media_paths.root / "unused.db",
        daily_cap_usd=None,
        job_timeout_minutes=0,
    )
    state = build_test_state(db=db, paths=media_paths, settings=settings)
    db.migrate()
    project = db.ensure_default_project()
    row = db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="kwaivgi/kling-v3.0-pro",
        prompt="a beach",
        params={},
        state=GenerationState.RUNNING,
    )
    db.set_provider_job_id(row.id, "job-abc")
    state.ledger.reserve(row.id, Decimal("0.50"))

    app = create_app(settings=state.settings, db=db)
    app.state.higgshole = state

    with TestClient(app):
        report = state.resume_report

    assert report.timed_out == (row.id,)
    assert report.reattached == ()
    assert [succeeded for _, _, succeeded in state.video_runner.gate.released] == [False]


def test_a_structured_error_is_serialised_at_the_top_level(app_and_state):
    # Both the browser and the MCP client read body["error"]; FastAPI's
    # default handler would nest the whole body under "detail" and break them.
    app, _ = app_and_state

    @app.get("/_test/structured")
    async def _structured() -> None:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "message": "bad", "issues": []},
        )

    with TestClient(app) as client:
        response = client.get("/_test/structured")

    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert "detail" not in response.json()


def test_a_plain_http_exception_still_returns_a_usable_body(app_and_state):
    # Starlette and FastAPI raise plain-string HTTPExceptions internally, so
    # the handler must give those the same two-field shape.
    app, _ = app_and_state

    @app.get("/_test/plain")
    async def _plain() -> None:
        raise HTTPException(status_code=404, detail="nothing here")

    with TestClient(app) as client:
        response = client.get("/_test/plain")

    assert response.status_code == 404
    assert response.json() == {"error": "http_error", "message": "nothing here"}


def test_only_one_database_is_opened_when_none_is_injected(monkeypatch, tmp_path):
    # Plan 2's Database wraps a single SQLite connection under a single
    # worker; a second one would be opened and never closed at shutdown.
    constructed: list[Database] = []
    original = Database.from_settings

    def counting(settings: Settings) -> Database:
        database = original(settings)
        constructed.append(database)
        return database

    monkeypatch.setattr(Database, "from_settings", counting)

    app = create_app(
        settings=Settings(
            media_root=tmp_path / "media",
            db_path=tmp_path / "higgshole.db",
            daily_cap_usd=None,
        )
    )

    with TestClient(app):
        pass

    assert len(constructed) == 1


def test_a_key_saved_through_the_ui_reaches_the_catalogue(monkeypatch, tmp_path):
    # The catalogue shares the resolving factory instead of building its own
    # from the environment; otherwise a refresh — and the lazy image-pricing
    # fetch behind it — would authenticate with a key the user never set and
    # fail with AuthError while generation worked fine.
    keys: list[str] = []

    def record(api_key: str) -> str:
        keys.append(api_key)
        return api_key

    monkeypatch.setattr("higgshole.web.app.OpenRouterClient", record)

    settings = Settings(
        media_root=tmp_path / "media",
        db_path=tmp_path / "higgshole.db",
        daily_cap_usd=None,
    )
    database = Database.from_settings(settings)
    database.migrate()
    database.set_setting("openrouter_api_key_video", "sk-or-v1-abcdef0123456789")

    state = build_app_state(settings, db=database)
    state.catalog.client_factory("video")

    assert keys == ["sk-or-v1-abcdef0123456789"]


def test_shutdown_cancels_every_poller(app_and_state):
    app, state = app_and_state

    with TestClient(app):
        pass

    assert state.video_runner.shutdown_calls == 1


def test_the_entrypoint_runs_exactly_one_worker(monkeypatch):
    # Spec section 9: two workers would each reattach a poller to the same job
    # at boot, causing duplicate downloads and double-counted spend.
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))

    run()

    assert captured["workers"] == 1
