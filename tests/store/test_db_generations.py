import sqlite3

import pytest

from higgshole.store.db import (
    AssetKind,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationState,
    IdCollisionError,
    InputRole,
    MediaFilter,
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def project(db):
    return db.get_project_by_slug("unsorted")


def make_generation(db, project, **overrides):
    kwargs = {
        "project_id": project.id,
        "kind": GenerationKind.IMAGE,
        "model": "openai/gpt-image-2",
        "prompt": "a cat",
        "params": {},
    }
    kwargs.update(overrides)
    return db.create_generation(**kwargs)


def test_create_generation_starts_pending(db, project):
    row = make_generation(db, project)

    assert row.state is GenerationState.PENDING
    assert row.kind is GenerationKind.IMAGE
    assert len(row.id) == 12
    assert row.completed_at is None
    assert db.get_generation(row.id) == row


def test_create_generation_round_trips_params_json(db, project):
    row = make_generation(db, project, params={"aspect_ratio": "16:9", "seed": 7})

    assert db.get_generation(row.id).params == {"aspect_ratio": "16:9", "seed": 7}


def test_create_generation_retries_on_id_collision(db, project, monkeypatch):
    taken = make_generation(db, project).id
    ids = iter([taken, taken, "b7e004aa1c32"])
    monkeypatch.setattr("higgshole.store.db.new_id", lambda: next(ids))

    row = make_generation(db, project)

    assert row.id == "b7e004aa1c32"


def test_id_collision_exhaustion_raises(db, project, monkeypatch):
    taken = make_generation(db, project).id
    monkeypatch.setattr("higgshole.store.db.new_id", lambda: taken)

    with pytest.raises(IdCollisionError):
        make_generation(db, project)


def test_set_generation_state_stamps_updated_at(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(row.id, GenerationState.GENERATING)

    assert updated.state is GenerationState.GENERATING
    assert updated.updated_at >= row.updated_at
    assert updated.completed_at is None


def test_terminal_state_stamps_completed_at(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(row.id, GenerationState.COMPLETE)

    assert updated.completed_at is not None


def test_set_generation_state_records_the_error_reason(db, project):
    row = make_generation(db, project)

    updated = db.set_generation_state(
        row.id,
        GenerationState.FAILED,
        error_reason=ErrorReason.INDETERMINATE,
        error_detail="connection reset after submit",
    )

    assert updated.error_reason is ErrorReason.INDETERMINATE
    assert updated.error_detail == "connection reset after submit"


def test_set_provider_job_id(db, project):
    row = make_generation(db, project, kind=GenerationKind.VIDEO)

    db.set_provider_job_id(row.id, "job-abc")

    assert db.get_generation(row.id).provider_job_id == "job-abc"


def test_duplicate_provider_job_id_is_rejected(db, project):
    # Spec section 9: double reattachment must be a database error, not a
    # duplicate download.
    first = make_generation(db, project, kind=GenerationKind.VIDEO)
    second = make_generation(db, project, kind=GenerationKind.VIDEO)
    db.set_provider_job_id(first.id, "job-abc")

    with pytest.raises(sqlite3.IntegrityError):
        db.set_provider_job_id(second.id, "job-abc")


def test_multiple_null_provider_job_ids_are_allowed(db, project):
    make_generation(db, project)
    make_generation(db, project)

    assert db.count_generations(MediaFilter()) == 2


def test_set_generation_file(db, project):
    row = make_generation(db, project)

    db.set_generation_file(row.id, "projects/unsorted/images/x.png")

    assert db.get_generation(row.id).file_path == "projects/unsorted/images/x.png"


def test_list_generations_newest_first(db, project):
    first = make_generation(db, project, prompt="one")
    second = make_generation(db, project, prompt="two")

    listed = db.list_generations(MediaFilter())

    assert [g.id for g in listed] == [second.id, first.id]


def test_list_generations_filters_by_kind(db, project):
    make_generation(db, project)
    video = make_generation(db, project, kind=GenerationKind.VIDEO)

    listed = db.list_generations(MediaFilter(kind=GenerationKind.VIDEO))

    assert [g.id for g in listed] == [video.id]


def test_list_generations_filters_by_project_slug(db, project):
    other = db.create_project(name="Other")
    make_generation(db, project)
    elsewhere = make_generation(db, other)

    listed = db.list_generations(MediaFilter(project_slug="other"))

    assert [g.id for g in listed] == [elsewhere.id]


def test_list_generations_filters_by_date_window(db, project):
    row = make_generation(db, project)
    created = db.get_generation(row.id).created_at

    assert db.list_generations(MediaFilter(created_after=created)) != []
    assert db.list_generations(MediaFilter(created_before=created)) == []


def test_count_generations_ignores_pagination(db, project):
    for _ in range(5):
        make_generation(db, project)

    listed = db.list_generations(MediaFilter(limit=2, offset=1))

    assert len(listed) == 2
    assert db.count_generations(MediaFilter(limit=2, offset=1)) == 5


def test_list_generations_in_states(db, project):
    pending = make_generation(db, project, kind=GenerationKind.VIDEO)
    running = make_generation(db, project, kind=GenerationKind.VIDEO)
    db.set_generation_state(running.id, GenerationState.RUNNING)
    make_generation(db, project)

    found = db.list_generations_in_states(
        [GenerationState.RUNNING], kind=GenerationKind.VIDEO
    )

    assert [g.id for g in found] == [running.id]
    assert pending.id not in {g.id for g in found}


def test_count_in_flight_excludes_terminal_states(db, project):
    make_generation(db, project)
    done = make_generation(db, project)
    db.set_generation_state(done.id, GenerationState.COMPLETE)
    rejected = make_generation(db, project)
    db.set_generation_state(rejected.id, GenerationState.REJECTED)

    assert db.count_in_flight() == 1


def test_create_and_get_asset(db, project):
    generation = make_generation(db, project)

    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="projects/unsorted/images/x.png",
        mime_type="image/png",
        bytes_=1024,
        generation_id=generation.id,
        width=1920,
        height=1080,
    )

    assert db.get_asset(asset.id) == asset
    assert asset.duration_s is None


def test_get_asset_by_path(db):
    asset = db.create_asset(
        kind=AssetKind.UPLOAD,
        file_path="projects/unsorted/uploads/ref.png",
        mime_type="image/png",
        bytes_=10,
    )

    assert db.get_asset_by_path("projects/unsorted/uploads/ref.png") == asset
    assert db.get_asset_by_path("nope") is None


def test_list_assets_for_generation(db, project):
    generation = make_generation(db, project)
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(
        kind=AssetKind.THUMBNAIL,
        file_path="t.webp",
        mime_type="image/webp",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1)

    assert {a.kind for a in db.list_assets_for_generation(generation.id)} == {
        AssetKind.OUTPUT,
        AssetKind.THUMBNAIL,
    }


def test_list_uploads(db, project):
    generation = make_generation(db, project)
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    upload = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1
    )

    assert [a.id for a in db.list_uploads()] == [upload.id]


def test_delete_asset_returns_its_path(db):
    asset = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u.png", mime_type="image/png", bytes_=1
    )

    assert db.delete_asset(asset.id) == "u.png"
    assert db.delete_asset(asset.id) is None


def test_delete_generation_returns_every_file_path(db, project):
    generation = make_generation(db, project)
    db.set_generation_file(generation.id, "projects/unsorted/images/x.png")
    db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="projects/unsorted/images/x.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )
    db.create_asset(
        kind=AssetKind.THUMBNAIL,
        file_path="thumbs/unsorted/x.webp",
        mime_type="image/webp",
        bytes_=1,
        generation_id=generation.id,
    )

    paths = db.delete_generation(generation.id)

    assert set(paths) == {
        "projects/unsorted/images/x.png",
        "thumbs/unsorted/x.webp",
    }


def test_delete_generation_cascades_assets(db, project):
    generation = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=generation.id,
    )

    db.delete_generation(generation.id)

    assert db.get_generation(generation.id) is None
    assert db.get_asset(asset.id) is None


def test_delete_generation_is_refused_when_an_asset_feeds_another_generation(db, project):
    # generation_inputs.asset_id is ON DELETE RESTRICT: lineage is not silently
    # discarded (spec section 4.5).
    parent = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=parent.id,
    )
    child = make_generation(db, project)
    db.add_generation_input(
        generation_id=child.id,
        asset_id=asset.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.delete_generation(parent.id)


def test_add_and_list_generation_inputs(db, project):
    generation = make_generation(db, project)
    first = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u1.png", mime_type="image/png", bytes_=1
    )
    second = db.create_asset(
        kind=AssetKind.UPLOAD, file_path="u2.png", mime_type="image/png", bytes_=1
    )
    db.add_generation_input(
        generation_id=generation.id,
        asset_id=second.id,
        role=InputRole.INPUT_REFERENCE,
        position=1,
    )
    db.add_generation_input(
        generation_id=generation.id,
        asset_id=first.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    inputs = db.list_generation_inputs(generation.id)

    assert [i.asset_id for i in inputs] == [first.id, second.id]
    assert inputs[0].role is InputRole.INPUT_REFERENCE


def test_list_generation_children(db, project):
    parent = make_generation(db, project)
    asset = db.create_asset(
        kind=AssetKind.OUTPUT,
        file_path="a.png",
        mime_type="image/png",
        bytes_=1,
        generation_id=parent.id,
    )
    child = make_generation(db, project, prompt="make it watercolour")
    db.add_generation_input(
        generation_id=child.id,
        asset_id=asset.id,
        role=InputRole.INPUT_REFERENCE,
        position=0,
    )

    assert [g.id for g in db.list_generation_children(asset.id)] == [child.id]
