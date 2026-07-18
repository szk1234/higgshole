from datetime import date
from decimal import Decimal

import pytest

from higgshole.budget.ledger import Ledger, utc_day_bounds
from higgshole.store.db import Database, GenerationKind, LedgerKind


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def gen_id(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    ).id


@pytest.fixture
def ledger(db):
    return Ledger(db)


def freeze(monkeypatch, iso):
    monkeypatch.setattr("higgshole.store.db.utc_now_iso", lambda: iso)


def test_reserve_appends_a_positive_row(ledger, gen_id):
    row = ledger.reserve(gen_id, Decimal("2.00"))

    assert row.kind is LedgerKind.RESERVATION
    assert row.amount == Decimal("2.00")
    assert ledger.outstanding_reservations() == Decimal("2.00")


def test_reverse_negates_the_outstanding_reservation(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    row = ledger.reverse(gen_id)

    assert row is not None
    assert row.amount == Decimal("-2.00")
    assert ledger.outstanding_reservations() == Decimal("0")


def test_reverse_twice_returns_none(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.reverse(gen_id)

    assert ledger.reverse(gen_id) is None


def test_reverse_with_no_reservation_returns_none(ledger, gen_id):
    assert ledger.reverse(gen_id) is None


def test_record_actual_reverses_then_records(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    row = ledger.record_actual(gen_id, Decimal("0.25"))

    assert row.kind is LedgerKind.ACTUAL
    assert row.cost_known is True
    kinds = [r.kind for r in ledger.db.list_ledger_for_generation(gen_id)]
    assert kinds == [LedgerKind.RESERVATION, LedgerKind.REVERSAL, LedgerKind.ACTUAL]


def test_a_completed_job_nets_to_its_actual_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))

    assert ledger.spend_for_day().total == Decimal("0.25")


def test_a_null_cost_leaves_the_reservation_standing(ledger, gen_id):
    # Spec section 3.4: the reservation stands as the recorded charge.
    ledger.reserve(gen_id, Decimal("2.00"))

    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().total == Decimal("2.00")
    assert ledger.outstanding_reservations() == Decimal("2.00")


def test_a_null_cost_marks_the_day_as_a_lower_bound(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().is_lower_bound is True


def test_zero_is_never_recorded_as_the_charge(ledger, gen_id):
    # Recording zero would let the cap silently never trip.
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.spend_for_day().total != Decimal("0")


def test_settle_failed_nets_to_zero(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    ledger.settle_failed(gen_id)

    assert ledger.spend_for_day().total == Decimal("0")
    assert ledger.spend_for_day().is_lower_bound is False


def test_spend_for_day_sums_signed_amounts(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))
    ledger.reserve(gen_id, Decimal("1.00"))

    assert ledger.spend_for_day().total == Decimal("1.25")


def test_spend_for_day_excludes_other_days(ledger, gen_id, monkeypatch):
    freeze(monkeypatch, "2026-07-17T23:59:59.999999+00:00")
    ledger.reserve(gen_id, Decimal("5.00"))
    freeze(monkeypatch, "2026-07-18T09:00:00.000000+00:00")
    ledger.reserve(gen_id, Decimal("1.00"))

    assert ledger.spend_for_day(date(2026, 7, 18)).total == Decimal("1.00")
    assert ledger.spend_for_day(date(2026, 7, 17)).total == Decimal("5.00")


def test_spend_for_day_reports_outstanding_reservations(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))

    assert ledger.spend_for_day().reserved == Decimal("2.00")

    ledger.reverse(gen_id)

    assert ledger.spend_for_day().reserved == Decimal("0")


def test_outstanding_reservations_across_all_time(ledger, db, gen_id, monkeypatch):
    freeze(monkeypatch, "2026-07-01T00:00:00.000000+00:00")
    ledger.reserve(gen_id, Decimal("2.00"))
    freeze(monkeypatch, "2026-07-18T00:00:00.000000+00:00")

    # The cap window does not reset on restart, and nor does this figure.
    assert ledger.outstanding_reservations() == Decimal("2.00")
    assert ledger.spend_for_day(date(2026, 7, 18)).total == Decimal("0")


def test_generation_total_reports_known_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, Decimal("0.25"))

    assert ledger.generation_total(gen_id) == (Decimal("0.25"), True)


def test_generation_total_reports_unknown_cost(ledger, gen_id):
    ledger.reserve(gen_id, Decimal("2.00"))
    ledger.record_actual(gen_id, None)

    assert ledger.generation_total(gen_id) == (Decimal("2.00"), False)


def test_generation_total_of_an_unrecorded_generation(ledger, gen_id):
    # No actual row means the cost is unknown, not zero.
    assert ledger.generation_total(gen_id) == (Decimal("0"), False)


def test_utc_day_bounds_is_half_open():
    start, end = utc_day_bounds(date(2026, 7, 18))

    assert start == "2026-07-18T00:00:00+00:00"
    assert end == "2026-07-19T00:00:00+00:00"


def test_utc_day_bounds_string_compare_includes_midnight():
    # Timestamps carry microseconds ('.') where the bounds carry an offset
    # ('+'), and '.' sorts after '+', so a midnight event lands inside its own
    # day and outside the next one under plain string comparison.
    start, end = utc_day_bounds(date(2026, 7, 18))
    midnight = "2026-07-18T00:00:00.000000+00:00"
    next_midnight = "2026-07-19T00:00:00.000000+00:00"

    assert start <= midnight < end
    assert not (start <= next_midnight < end)


def test_ledger_is_append_only_across_a_reopen(tmp_path, monkeypatch):
    path = tmp_path / "state" / "higgshole.db"
    with Database(path) as first:
        first.migrate()
        project = first.get_project_by_slug("unsorted")
        generation = first.create_generation(
            project_id=project.id,
            kind=GenerationKind.IMAGE,
            model="a/b",
            prompt="p",
            params={},
        )
        Ledger(first).reserve(generation.id, Decimal("2.00"))
        Ledger(first).record_actual(generation.id, Decimal("0.25"))

    with Database(path) as second:
        rows = second.list_ledger_for_generation(generation.id)

    assert [r.kind for r in rows] == [
        LedgerKind.RESERVATION,
        LedgerKind.REVERSAL,
        LedgerKind.ACTUAL,
    ]
    assert Ledger(Database(path)).generation_total(generation.id) == (
        Decimal("0.25"),
        True,
    )
