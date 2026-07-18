import asyncio
from decimal import Decimal

import pytest

from higgshole.budget.estimator import Estimate, EstimateUnavailable
from higgshole.budget.gate import BudgetGate, GateDecision, GateRejection, Reservation
from higgshole.budget.ledger import Ledger
from higgshole.config import Settings
from higgshole.orclient.types import KeyStatus
from higgshole.store.db import Database, GenerationKind, GenerationState

EXACT = Estimate(amount=Decimal("0.28"), reason=None, detail="5s at 0.056/s")
UNKNOWN = Estimate(
    amount=None,
    reason=EstimateUnavailable.VIDEO_TOKEN_PRICED,
    detail="priced per video token",
)


@pytest.fixture
def db():
    with Database.in_memory() as database:
        database.migrate()
        yield database


@pytest.fixture
def ledger(db):
    return Ledger(db)


def new_generation(db):
    project = db.get_project_by_slug("unsorted")
    return db.create_generation(
        project_id=project.id,
        kind=GenerationKind.VIDEO,
        model="google/veo-3.1",
        prompt="a beach",
        params={},
    ).id


def make_gate(db, ledger, *, cap="10.00", ceiling="2.00", in_flight=3):
    return BudgetGate(
        db,
        ledger,
        daily_cap_usd=None if cap is None else Decimal(cap),
        max_job_cost_usd=Decimal(ceiling),
        max_in_flight=in_flight,
    )


async def test_an_exact_estimate_reserves_the_estimate(db, ledger):
    gate = make_gate(db, ledger)
    gen_id = new_generation(db)

    granted = await gate.acquire(generation_id=gen_id, estimate=EXACT)

    assert isinstance(granted, Reservation)
    assert granted.amount == Decimal("0.28")
    assert granted.from_exact_estimate is True
    assert granted.ledger_row_id > 0
    assert ledger.spend_for_day().total == Decimal("0.28")


async def test_a_non_estimable_job_reserves_the_ceiling(db, ledger):
    # Spec section 3.3: the pessimistic ceiling stands in for an estimate.
    gate = make_gate(db, ledger, ceiling="2.00")
    gen_id = new_generation(db)

    granted = await gate.acquire(generation_id=gen_id, estimate=UNKNOWN)

    assert granted.amount == Decimal("2.00")
    assert granted.from_exact_estimate is False


async def test_the_cap_rejects_a_job_that_would_exceed_it(db, ledger):
    gate = make_gate(db, ledger, cap="3.00", ceiling="2.00", in_flight=10)
    await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    assert isinstance(rejection, GateRejection)
    assert rejection.decision is GateDecision.CAP_EXCEEDED


async def test_a_rejection_reports_the_remaining_balance(db, ledger):
    gate = make_gate(db, ledger, cap="3.00", ceiling="2.00", in_flight=10)
    await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    assert rejection.cap == Decimal("3.00")
    assert rejection.spent_today == Decimal("2.00")
    assert rejection.remaining_today == Decimal("1.00")
    assert rejection.would_reserve == Decimal("2.00")
    assert "cap" in rejection.message.lower()


async def test_no_cap_means_no_cap_rejection(db, ledger):
    gate = make_gate(db, ledger, cap=None, ceiling="2.00", in_flight=10)

    for _ in range(5):
        granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)
        assert isinstance(granted, Reservation)


async def test_the_in_flight_ceiling_rejects(db, ledger):
    gate = make_gate(db, ledger, in_flight=1)
    new_generation(db)  # a second row occupying a non-terminal state

    rejection = await gate.acquire(generation_id=new_generation(db), estimate=EXACT)

    assert rejection.decision is GateDecision.IN_FLIGHT_LIMIT


async def test_the_generation_being_gated_does_not_count_against_itself(db, ledger):
    gate = make_gate(db, ledger, in_flight=1)

    granted = await gate.acquire(generation_id=new_generation(db), estimate=EXACT)

    assert isinstance(granted, Reservation)


async def test_concurrent_acquisitions_cannot_exceed_the_cap(db, ledger):
    # Spec section 3.3: without the lock, ten submissions in one second would
    # each observe the same remaining balance.
    gate = make_gate(db, ledger, cap="5.00", ceiling="2.00", in_flight=100)
    ids = [new_generation(db) for _ in range(10)]

    results = await asyncio.gather(
        *(gate.acquire(generation_id=gen_id, estimate=UNKNOWN) for gen_id in ids)
    )

    granted = [r for r in results if isinstance(r, Reservation)]
    assert len(granted) == 2
    assert ledger.spend_for_day().total == Decimal("4.00")
    assert ledger.spend_for_day().total <= Decimal("5.00")


async def test_release_on_success_records_the_actual_cost(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=Decimal("0.25"), succeeded=True)

    assert ledger.generation_total(granted.generation_id) == (Decimal("0.25"), True)


async def test_release_on_success_with_no_cost_leaves_the_reservation(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=None, succeeded=True)

    assert ledger.generation_total(granted.generation_id) == (Decimal("2.00"), False)
    assert ledger.spend_for_day().is_lower_bound is True


async def test_release_on_failure_nets_to_zero(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)

    await gate.release(granted, actual_cost=None, succeeded=False)

    assert ledger.spend_for_day().total == Decimal("0")


def test_cap_is_set_reflects_configuration(db, ledger):
    # Spec section 3.5: quality=auto is refused whenever a cap exists, at any
    # remaining balance.
    assert make_gate(db, ledger, cap="1.00").cap_is_set is True
    assert make_gate(db, ledger, cap=None).cap_is_set is False
    assert make_gate(db, ledger, cap="1.00").cap == Decimal("1.00")


async def test_status_uses_provider_figures_when_available(db, ledger):
    gate = make_gate(db, ledger, cap="10.00")
    await gate.acquire(generation_id=new_generation(db), estimate=EXACT)
    key_status = KeyStatus.from_api(
        {"data": {"limit": 100, "limit_remaining": 74.5, "usage_daily": 25.5}}
    )

    status = await gate.status(key_status)

    assert status.provider_available is True
    assert status.provider_remaining == Decimal("74.5")
    assert status.provider_usage_daily == Decimal("25.5")
    assert status.spent_today == Decimal("0.28")
    assert status.remaining_today == Decimal("9.72")
    assert status.max_in_flight == 3


async def test_status_marks_provider_unavailable_when_the_key_call_failed(db, ledger):
    # Spec section 3.2: the UI then labels the figures local-only.
    gate = make_gate(db, ledger)

    status = await gate.status(None)

    assert status.provider_available is False
    assert status.provider_limit is None
    assert status.provider_remaining is None


async def test_status_reports_a_lower_bound_day(db, ledger):
    gate = make_gate(db, ledger)
    granted = await gate.acquire(generation_id=new_generation(db), estimate=UNKNOWN)
    await gate.release(granted, actual_cost=None, succeeded=True)

    status = await gate.status(None)

    assert status.is_lower_bound is True
    assert status.spent_today == Decimal("2.00")


def test_from_settings_reads_the_cap_and_ceilings(db, ledger, monkeypatch):
    monkeypatch.setenv("HIGGSHOLE_DAILY_CAP_USD", "7.50")
    monkeypatch.setenv("HIGGSHOLE_MAX_JOB_COST_USD", "3.00")
    monkeypatch.setenv("HIGGSHOLE_MAX_IN_FLIGHT", "5")

    gate = BudgetGate.from_settings(db, ledger, Settings())

    assert gate.cap == Decimal("7.50")
    assert gate._max_job_cost_usd == Decimal("3.00")
    assert gate._max_in_flight == 5


async def test_a_rejection_is_returned_not_raised(db, ledger):
    # Rejection is a normal budget outcome, not an error condition.
    gate = make_gate(db, ledger, cap="0.10", ceiling="2.00", in_flight=10)
    gen_id = new_generation(db)

    result = await gate.acquire(generation_id=gen_id, estimate=UNKNOWN)

    assert isinstance(result, GateRejection)
    assert db.get_generation(gen_id).state is GenerationState.PENDING
    assert ledger.spend_for_day().total == Decimal("0")
