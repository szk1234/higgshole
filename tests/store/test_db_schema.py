import os
import sqlite3

import pytest

from higgshole.config import Settings
from higgshole.store.db import (
    IMAGE_STATES,
    RESUMABLE_STATES,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    VIDEO_STATES,
    Database,
    DuplicateSlugError,
    GenerationKind,
    GenerationState,
    ProjectNotEmptyError,
    utc_now_iso,
)

EXPECTED_TABLES = {
    "projects",
    "generations",
    "assets",
    "generation_inputs",
    "spend_ledger",
    "model_catalog",
    "model_pricing",
    "settings",
}


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


def test_migrate_creates_all_eight_tables(db):
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert EXPECTED_TABLES <= {row[0] for row in rows}


def test_migrate_is_idempotent(db):
    db.migrate()
    db.migrate()

    assert db.get_setting("schema_version") == str(SCHEMA_VERSION)


def test_migrate_records_the_schema_version(db):
    assert db.get_setting("schema_version") == str(SCHEMA_VERSION)


def test_migrate_creates_the_unsorted_project(db):
    # Spec section 5.1: agent generation must never fail for want of a project.
    project = db.get_project_by_slug("unsorted")

    assert project is not None
    assert project.name == "Unsorted"


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO generations (id, project_id, kind, model, prompt,"
                " params, state, created_at, updated_at)"
                " VALUES ('x', 'no-such-project', 'image', 'a/b', 'p', '{}',"
                " 'PENDING', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')"
            )


def test_utc_now_iso_is_utc_with_offset():
    value = utc_now_iso()

    assert value.endswith("+00:00")
    assert value[4] == "-" and value[10] == "T"


def test_state_sets_partition_by_kind():
    assert GenerationState.GENERATING in IMAGE_STATES
    assert GenerationState.GENERATING not in VIDEO_STATES
    assert GenerationState.RUNNING in VIDEO_STATES
    assert GenerationState.RUNNING not in IMAGE_STATES


def test_terminal_and_resumable_state_sets():
    assert TERMINAL_STATES == {
        GenerationState.COMPLETE,
        GenerationState.FAILED,
        GenerationState.REJECTED,
    }
    assert RESUMABLE_STATES == {GenerationState.SUBMITTED, GenerationState.RUNNING}
    assert not RESUMABLE_STATES & TERMINAL_STATES


def test_create_project_slugifies_the_name(db):
    project = db.create_project(name="My Great Project!")

    assert project.slug == "my-great-project"
    assert project.name == "My Great Project!"


def test_duplicate_slug_is_rejected(db):
    db.create_project(name="Demo")

    with pytest.raises(DuplicateSlugError):
        db.create_project(name="demo")


def test_get_project_by_slug(db):
    created = db.create_project(name="Demo")

    assert db.get_project_by_slug("demo") == created
    assert db.get_project(created.id) == created
    assert db.get_project_by_slug("absent") is None


def test_list_projects_puts_unsorted_first(db):
    db.create_project(name="Alpha")
    db.create_project(name="Beta")

    assert [p.slug for p in db.list_projects()][0] == "unsorted"
    assert {p.slug for p in db.list_projects()} == {"unsorted", "alpha", "beta"}


def test_delete_project_refuses_when_generations_exist(db):
    project = db.create_project(name="Demo")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO generations (id, project_id, kind, model, prompt, params,"
            " state, created_at, updated_at) VALUES (?, ?, ?, 'a/b', 'p', '{}',"
            " 'PENDING', ?, ?)",
            (
                "a3f21c9d4e07",
                project.id,
                GenerationKind.IMAGE.value,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )

    with pytest.raises(ProjectNotEmptyError):
        db.delete_project(project.id)


def test_delete_project_removes_an_empty_project(db):
    project = db.create_project(name="Demo")

    db.delete_project(project.id)

    assert db.get_project(project.id) is None


def test_settings_key_value_round_trip(db):
    db.set_setting("daily_cap_usd", "12.50")
    db.set_setting("daily_cap_usd", "20.00")

    assert db.get_setting("daily_cap_usd") == "20.00"
    assert db.get_setting("absent") is None


def test_delete_setting_and_all_settings(db):
    db.set_setting("favourite_models", '["a/b"]')
    db.delete_setting("favourite_models")
    db.delete_setting("favourite_models")

    assert db.get_setting("favourite_models") is None
    assert db.all_settings()["schema_version"] == str(SCHEMA_VERSION)


def test_database_file_is_created_with_owner_only_permissions(tmp_path, monkeypatch):
    # Spec section 7: the database holds API keys.
    monkeypatch.setenv("HIGGSHOLE_DB_PATH", str(tmp_path / "state" / "higgshole.db"))
    settings = Settings()

    with Database.from_settings(settings) as database:
        database.migrate()

    assert os.stat(settings.db_path).st_mode & 0o777 == 0o600
