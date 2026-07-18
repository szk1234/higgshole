"""The local spend record: append-only, signed amounts, summed in Python.

Every terminal state appends a reversal, so spend for a window is the plain
sum of `amount` and can never be double-counted (spec 3.3). The one deliberate
exception is a completed job that reports no cost: its reservation stands as
the recorded charge and the day is marked as a lower bound, because recording
zero would let the cap silently never trip (spec 3.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from higgshole.store.db import Database, LedgerKind, LedgerRow

ZERO = Decimal("0")


@dataclass(frozen=True)
class DaySpend:
    """Spend across one UTC calendar day."""

    day: date
    total: Decimal
    is_lower_bound: bool
    reserved: Decimal


@dataclass(frozen=True)
class BudgetStatus:
    """What get_budget returns (spec 3.2).

    Provider figures are authoritative; ledger figures govern only the local
    cap.
    """

    provider_limit: Decimal | None
    provider_remaining: Decimal | None
    provider_usage_daily: Decimal | None
    provider_available: bool
    cap: Decimal | None
    spent_today: Decimal
    remaining_today: Decimal | None
    is_lower_bound: bool
    in_flight: int
    max_in_flight: int


def utc_day_bounds(day: date) -> tuple[str, str]:
    """[start, end) ISO-8601 UTC strings bounding one calendar day.

    Comparison against recorded_at is a plain string comparison under SQLite's
    BINARY collation. That is exact here because every stored timestamp shares
    the same fixed-width prefix, and because '.' (microseconds) sorts after
    '+' (the offset), so a timestamp at exactly midnight falls inside its own
    day and outside the following one.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


class Ledger:
    """Append-only signed-amount ledger.

    Every method is a pure function of the rows in spend_ledger plus the
    clock; no in-memory state survives a restart, which is what allows the cap
    window to keep counting across one.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        """The underlying database. Exposed for callers that must read raw
        ledger rows, such as reservation recovery at boot."""
        return self._db

    def _outstanding_for(self, generation_id: str) -> Decimal:
        """Reservations for one generation, net of any reversals."""
        return sum(
            (
                row.amount
                for row in self._db.list_ledger_for_generation(generation_id)
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )

    def reserve(self, generation_id: str, amount: Decimal) -> LedgerRow:
        """Append a positive `reservation`. Called only inside the gate's lock.

        cost_known is False: a reservation is a ceiling, not an observation.
        """
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.RESERVATION,
            amount=amount,
            cost_known=False,
        )

    def reverse(self, generation_id: str) -> LedgerRow | None:
        """Append a `reversal` negating the outstanding reservation.

        Returns None when there is nothing to reverse (already reversed, or
        none was taken). Called on EVERY terminal state.
        """
        outstanding = self._outstanding_for(generation_id)
        if outstanding <= ZERO:
            return None
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.REVERSAL,
            amount=-outstanding,
            cost_known=False,
        )

    def record_actual(self, generation_id: str, cost: Decimal | None) -> LedgerRow:
        """Record the provider-reported cost.

        cost is not None -> reverse() then append `actual` with cost_known=1.
        cost is None     -> the reservation STANDS as the recorded charge; an
                            `actual` row of amount 0 with cost_known=0 is
                            appended to mark the day as a lower bound. The
                            reservation is deliberately NOT reversed, and zero
                            is never recorded as the charge (spec 3.4).
        """
        if cost is None:
            return self._db.append_ledger(
                generation_id=generation_id,
                kind=LedgerKind.ACTUAL,
                amount=ZERO,
                cost_known=False,
            )

        self.reverse(generation_id)
        return self._db.append_ledger(
            generation_id=generation_id,
            kind=LedgerKind.ACTUAL,
            amount=cost,
            cost_known=True,
        )

    def settle_failed(self, generation_id: str) -> None:
        """Reverse the reservation with no actual, so a failed job nets to
        zero (spec 3.3)."""
        self.reverse(generation_id)

    def spend_for_day(self, day: date | None = None) -> DaySpend:
        """Sum signed amounts over one UTC calendar day of recorded_at.

        Defaults to today. The cap window does not reset on restart.
        """
        target = day or datetime.now(UTC).date()
        start, end = utc_day_bounds(target)
        rows = self._db.list_ledger_between(start_iso=start, end_iso=end)

        total = sum((row.amount for row in rows), ZERO)
        reserved = sum(
            (
                row.amount
                for row in rows
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )
        lower_bound = any(
            row.kind is LedgerKind.ACTUAL and not row.cost_known for row in rows
        )

        return DaySpend(
            day=target,
            total=total,
            is_lower_bound=lower_bound,
            reserved=reserved,
        )

    def outstanding_reservations(self) -> Decimal:
        """Sum of reservations with no matching reversal, across all time."""
        rows = self._db.list_ledger_between(start_iso="", end_iso="~")
        return sum(
            (
                row.amount
                for row in rows
                if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
            ),
            ZERO,
        )

    def generation_total(self, generation_id: str) -> tuple[Decimal, bool]:
        """(net amount, cost_known) for one generation.

        cost_known is False when no `actual` row exists at all — a rejected or
        failed generation has no cost, which is not the same as costing zero,
        and the caller renders it as None rather than as a price.
        """
        rows = self._db.list_ledger_for_generation(generation_id)
        total = sum((row.amount for row in rows), ZERO)
        actuals = [row for row in rows if row.kind is LedgerKind.ACTUAL]
        known = bool(actuals) and all(row.cost_known for row in actuals)
        return total, known
