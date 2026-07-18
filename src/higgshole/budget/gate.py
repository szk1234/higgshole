"""The serialized reservation gate (spec 3.3).

Estimate, cap check and reservation write happen inside ONE process-wide async
lock, so ten submissions in one second cannot each observe the same remaining
balance. The lock is process-local, which is why the deployment runs exactly
one uvicorn worker (spec 9).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from higgshole.config import Settings
from higgshole.orclient.types import KeyStatus
from higgshole.store.db import Database

from .estimator import Estimate, reservation_amount
from .ledger import BudgetStatus, Ledger

ZERO = Decimal("0")


@dataclass(frozen=True)
class Reservation:
    """A granted reservation. Held by the job runner until a terminal state."""

    generation_id: str
    amount: Decimal
    from_exact_estimate: bool
    ledger_row_id: int


class GateDecision(StrEnum):
    ALLOWED = "allowed"
    CAP_EXCEEDED = "cap_exceeded"
    IN_FLIGHT_LIMIT = "in_flight_limit"


@dataclass(frozen=True)
class GateRejection:
    decision: GateDecision
    message: str
    cap: Decimal | None
    spent_today: Decimal
    remaining_today: Decimal | None
    would_reserve: Decimal


class BudgetGate:
    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        *,
        daily_cap_usd: Decimal | None,
        max_job_cost_usd: Decimal,
        max_in_flight: int,
    ) -> None:
        self._db = db
        self._ledger = ledger
        self._cap = daily_cap_usd
        self._max_job_cost_usd = max_job_cost_usd
        self._max_in_flight = max_in_flight
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls, db: Database, ledger: Ledger, settings: Settings
    ) -> BudgetGate:
        return cls(
            db,
            ledger,
            daily_cap_usd=settings.daily_cap_usd,
            max_job_cost_usd=settings.max_job_cost_usd,
            max_in_flight=settings.max_in_flight,
        )

    @property
    def cap(self) -> Decimal | None:
        return self._cap

    def set_daily_cap(self, cap: Decimal | None) -> None:
        """Replace the cap without rebuilding the gate.

        The cap may be saved through the UI (spec section 8) while runners
        already hold this instance, so it has to be mutable in place. The
        assignment is atomic and `acquire` reads it under the lock, so a
        concurrent reservation sees either the old cap or the new one.
        """
        self._cap = cap

    @property
    def cap_is_set(self) -> bool:
        """Passed to validate_image_request(daily_cap_set=...).

        quality=auto is refused whenever a cap exists, at any remaining
        balance, because it is unbounded by definition (spec 3.5).
        """
        return self._cap is not None

    async def acquire(
        self, *, generation_id: str, estimate: Estimate
    ) -> Reservation | GateRejection:
        """Serialized: count in-flight, compute today's spend, reserve.

        Returns a Reservation on success, or a GateRejection which the caller
        turns into state REJECTED with the matching ErrorReason. Never raises
        for a budget outcome — rejection is a normal result, not an error.
        """
        async with self._lock:
            amount, exact = reservation_amount(
                estimate, max_job_cost_usd=self._max_job_cost_usd
            )
            day = self._ledger.spend_for_day()
            remaining = None if self._cap is None else self._cap - day.total

            # Excluded in SQL, not subtracted afterwards: exactly one exclusion
            # happens, so max_in_flight=3 still means three concurrent jobs.
            in_flight = self._db.count_in_flight(
                exclude_generation_id=generation_id
            )
            if in_flight >= self._max_in_flight:
                return GateRejection(
                    decision=GateDecision.IN_FLIGHT_LIMIT,
                    message=(
                        f"{in_flight} generation(s) already in flight; the ceiling "
                        f"is {self._max_in_flight}. Try again when one finishes."
                    ),
                    cap=self._cap,
                    spent_today=day.total,
                    remaining_today=remaining,
                    would_reserve=amount,
                )

            if self._cap is not None and day.total + amount > self._cap:
                return GateRejection(
                    decision=GateDecision.CAP_EXCEEDED,
                    message=(
                        f"the local daily cap of {self._cap} USD would be exceeded: "
                        f"{day.total} already recorded today and this job reserves "
                        f"{amount}."
                    ),
                    cap=self._cap,
                    spent_today=day.total,
                    remaining_today=remaining,
                    would_reserve=amount,
                )

            row = self._ledger.reserve(generation_id, amount)
            return Reservation(
                generation_id=generation_id,
                amount=amount,
                from_exact_estimate=exact,
                ledger_row_id=row.id,
            )

    async def release(
        self,
        reservation: Reservation,
        *,
        actual_cost: Decimal | None,
        succeeded: bool,
    ) -> None:
        """Settle on any terminal state.

        succeeded=False           -> ledger.settle_failed (nets to zero)
        succeeded, cost is None   -> ledger.record_actual(None): reservation
                                     stands, day marked as a lower bound
        succeeded, cost present   -> ledger.record_actual(cost)

        Held under the same lock as acquire so that a settlement cannot land
        between another submission's spend read and its reservation write.
        """
        async with self._lock:
            if not succeeded:
                self._ledger.settle_failed(reservation.generation_id)
                return
            self._ledger.record_actual(reservation.generation_id, actual_cost)

    async def status(self, key_status: KeyStatus | None) -> BudgetStatus:
        """Assemble BudgetStatus.

        `key_status=None` means the free GET /api/v1/key call failed, so
        provider_available is False and the UI labels the figures local-only
        (spec 3.2).
        """
        day = self._ledger.spend_for_day()
        return BudgetStatus(
            provider_limit=key_status.limit if key_status else None,
            provider_remaining=key_status.limit_remaining if key_status else None,
            provider_usage_daily=key_status.usage_daily if key_status else None,
            provider_available=key_status is not None,
            cap=self._cap,
            spent_today=day.total,
            remaining_today=None if self._cap is None else self._cap - day.total,
            is_lower_bound=day.is_lower_bound,
            in_flight=self._db.count_in_flight(),
            max_in_flight=self._max_in_flight,
        )
