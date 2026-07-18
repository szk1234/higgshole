from decimal import Decimal

import pytest

from higgshole.store.db import (
    Database,
    GenerationKind,
    GenerationState,
    LedgerKind,
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def generation(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    )


def test_append_ledger_stores_amount_as_text(db, generation):
    # SQLite REAL would round money; the contract mandates TEXT.
    row = db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.RESERVATION,
        amount=Decimal("2.00"),
        cost_known=False,
    )

    assert row.amount == Decimal("2.00")
    assert row.kind is LedgerKind.RESERVATION
    assert row.cost_known is False
    stored = db._conn.execute("SELECT amount FROM spend_ledger").fetchone()[0]
    assert isinstance(stored, str)
    assert stored == "2.00"


def test_negative_amounts_round_trip(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.REVERSAL,
        amount=Decimal("-2.00"),
        cost_known=False,
    )

    assert db.list_ledger_for_generation(generation.id)[0].amount == Decimal("-2.00")


def test_list_ledger_for_generation_in_order(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.RESERVATION,
        amount=Decimal("2.00"),
        cost_known=False,
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.REVERSAL,
        amount=Decimal("-2.00"),
        cost_known=False,
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("0.25"),
        cost_known=True,
    )

    kinds = [row.kind for row in db.list_ledger_for_generation(generation.id)]

    assert kinds == [LedgerKind.RESERVATION, LedgerKind.REVERSAL, LedgerKind.ACTUAL]


def test_list_ledger_between_is_half_open(db, generation, monkeypatch):
    monkeypatch.setattr(
        "higgshole.store.db.utc_now_iso", lambda: "2026-07-18T00:00:00+00:00"
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("1.00"),
        cost_known=True,
    )
    monkeypatch.setattr(
        "higgshole.store.db.utc_now_iso", lambda: "2026-07-19T00:00:00+00:00"
    )
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("2.00"),
        cost_known=True,
    )

    rows = db.list_ledger_between(
        start_iso="2026-07-18T00:00:00+00:00", end_iso="2026-07-19T00:00:00+00:00"
    )

    assert [row.amount for row in rows] == [Decimal("1.00")]


def test_ledger_cascades_when_a_generation_is_deleted(db, generation):
    db.append_ledger(
        generation_id=generation.id,
        kind=LedgerKind.ACTUAL,
        amount=Decimal("1.00"),
        cost_known=True,
    )
    db.set_generation_state(generation.id, GenerationState.COMPLETE)

    db.delete_generation(generation.id)

    assert db.list_ledger_for_generation(generation.id) == []


def test_upsert_catalog_round_trips_capabilities(db):
    db.upsert_catalog(
        model_id="google/veo-3.1",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "google/veo-3.1", "supported_durations": [4, 6, 8]},
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    row = db.get_catalog("google/veo-3.1", GenerationKind.VIDEO)

    assert row is not None
    assert row.capabilities["supported_durations"] == [4, 6, 8]
    assert row.kind is GenerationKind.VIDEO


def test_replace_catalog_replaces_the_whole_kind(db):
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("a/one", {"id": "a/one"}), ("a/two", {"id": "a/two"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("a/three", {"id": "a/three"})],
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    assert [r.model_id for r in db.list_catalog(GenerationKind.VIDEO)] == ["a/three"]


def test_replace_catalog_leaves_the_other_kind_alone(db):
    db.replace_catalog(
        GenerationKind.IMAGE,
        [("i/one", {"id": "i/one"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.replace_catalog(
        GenerationKind.VIDEO,
        [("v/one", {"id": "v/one"})],
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    assert [r.model_id for r in db.list_catalog(GenerationKind.IMAGE)] == ["i/one"]
    assert len(db.list_catalog()) == 2


def test_catalog_fetched_at_returns_the_oldest(db):
    db.upsert_catalog(
        model_id="a/one",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/one"},
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.upsert_catalog(
        model_id="a/two",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/two"},
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    assert db.catalog_fetched_at(GenerationKind.VIDEO) == "2026-07-18T00:00:00+00:00"


def test_catalog_fetched_at_is_none_when_empty(db):
    assert db.catalog_fetched_at(GenerationKind.IMAGE) is None


def test_get_catalog_by_id_and_kind(db):
    db.upsert_catalog(
        model_id="a/one",
        kind=GenerationKind.VIDEO,
        capabilities={"id": "a/one"},
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    assert db.get_catalog("a/one", GenerationKind.IMAGE) is None
    assert db.get_catalog("absent", GenerationKind.VIDEO) is None


def test_upsert_pricing_round_trips_a_line_item_array(db):
    items = [
        {"billable": "output_image", "unit": "image", "cost_usd": 0.06},
        {"billable": "input_reference", "unit": "image", "cost_usd": 0.20},
    ]

    db.upsert_pricing(
        model_id="riverflow/riverflow-v2-pro",
        pricing=items,
        fetched_at="2026-07-18T00:00:00+00:00",
    )

    row = db.get_pricing("riverflow/riverflow-v2-pro")

    assert row is not None
    assert row.pricing == items
    assert db.get_pricing("absent") is None


def test_upsert_pricing_overwrites(db):
    db.upsert_pricing(
        model_id="a/one",
        pricing=[{"billable": "output_image", "unit": "image", "cost_usd": 0.06}],
        fetched_at="2026-07-18T00:00:00+00:00",
    )
    db.upsert_pricing(
        model_id="a/one",
        pricing=[{"billable": "output_image", "unit": "image", "cost_usd": 0.08}],
        fetched_at="2026-07-19T00:00:00+00:00",
    )

    row = db.get_pricing("a/one")

    assert row.pricing[0]["cost_usd"] == 0.08
    assert row.fetched_at == "2026-07-19T00:00:00+00:00"
