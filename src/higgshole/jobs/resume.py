"""Boot-time poller reattachment (spec section 4.3).

Video pollers are in-process asyncio tasks. When the process stops, any job
still rendering upstream keeps rendering, and its row is left in SUBMITTED or
RUNNING. This module is what makes that survivable: at the next boot the rows
are found by state, their reservations are re-derived from the ledger, and a
poller is attached to each one.

Reservations are rebuilt from the ledger rather than remembered, so a restart
can neither leak a reservation (which would shrink the cap forever) nor
double-count one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from higgshole.budget.gate import Reservation
from higgshole.budget.ledger import Ledger
from higgshole.config import Settings
from higgshole.jobs.runner import VideoJobRunner
from higgshole.store.db import (
    RESUMABLE_STATES,
    Database,
    ErrorReason,
    GenerationKind,
    GenerationState,
    LedgerKind,
)


@dataclass(frozen=True)
class ResumeReport:
    """Emitted at startup and shown in Settings."""

    #: Generation IDs now being polled again.
    reattached: tuple[str, ...] = ()
    #: Exceeded the wall-clock ceiling while the service was down; FAILED.
    timed_out: tuple[str, ...] = ()
    #: In a resumable state but carrying no provider job ID; unrecoverable.
    orphaned: tuple[str, ...] = ()


def reservation_for(ledger: Ledger, gen_id: str) -> Reservation | None:
    """Rebuild the in-memory Reservation for a generation, or None if settled.

    Amounts are signed and the ledger is append-only, so "still outstanding"
    is simply reservations plus reversals being positive. Summing happens in
    Python because the amounts are Decimal strings — SQLite REAL would round
    money.

    `from_exact_estimate` is always rebuilt as False. Exactness is not
    persisted: `Ledger.reserve` writes `cost_known=False` on every reservation
    row, because a reservation is a ceiling rather than an observation, so the
    column cannot tell an exactly-estimated job from a pessimistic one. Reading
    it back would conflate two different booleans. False is safe because the
    flag only affects reporting — it never changes the amount reserved, which
    is recovered exactly from the summed ledger rows.
    """
    rows = ledger.db.list_ledger_for_generation(gen_id)
    reservations = [row for row in rows if row.kind is LedgerKind.RESERVATION]
    if not reservations:
        return None

    outstanding = sum(
        (
            row.amount
            for row in rows
            if row.kind in (LedgerKind.RESERVATION, LedgerKind.REVERSAL)
        ),
        Decimal("0"),
    )
    if outstanding <= Decimal("0"):
        return None

    latest = reservations[-1]
    return Reservation(
        generation_id=gen_id,
        amount=outstanding,
        # Not recoverable at boot; see the docstring. Reporting only.
        from_exact_estimate=False,
        ledger_row_id=latest.id,
    )


def _is_older_than(created_at: str, ceiling: timedelta) -> bool:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created >= ceiling


async def resume_pending_jobs(
    *,
    db: Database,
    runner: VideoJobRunner,
    ledger: Ledger,
    settings: Settings,
) -> ResumeReport:
    """Reattach pollers to video generations left mid-flight.

    Selects rows with kind='video' and state in RESUMABLE_STATES. Only video
    rows can occupy those states, but the kind filter is applied anyway as a
    defence against a corrupted row.

    A row already older than job_timeout_minutes is failed immediately with
    TIMEOUT rather than reattached. A row in a resumable state with
    provider_job_id NULL is unrecoverable — the process died between the gate
    and the submit response — and is failed with INDETERMINATE, since the
    submission may have been billed.
    """
    rows = db.list_generations_in_states(RESUMABLE_STATES, kind=GenerationKind.VIDEO)
    ceiling = timedelta(minutes=settings.job_timeout_minutes)

    reattached: list[str] = []
    timed_out: list[str] = []
    orphaned: list[str] = []

    for row in rows:
        reservation = reservation_for(ledger, row.id)

        if row.provider_job_id is None:
            db.set_generation_state(
                row.id,
                GenerationState.FAILED,
                error_reason=ErrorReason.INDETERMINATE,
                error_detail=(
                    "The service stopped between reserving budget and receiving "
                    "a job ID, so this submission may have been billed without "
                    "producing a recoverable job."
                ),
            )
            if reservation is not None:
                await runner.gate.release(
                    reservation, actual_cost=None, succeeded=False
                )
            orphaned.append(row.id)
            continue

        if _is_older_than(row.created_at, ceiling):
            db.set_generation_state(
                row.id,
                GenerationState.FAILED,
                error_reason=ErrorReason.TIMEOUT,
                error_detail=(
                    f"Job {row.provider_job_id} exceeded the "
                    f"{settings.job_timeout_minutes}-minute ceiling while the "
                    "service was not running."
                ),
            )
            if reservation is not None:
                await runner.gate.release(
                    reservation, actual_cost=None, succeeded=False
                )
            timed_out.append(row.id)
            continue

        runner.attach_poller(row.id, reservation=reservation)
        reattached.append(row.id)

    return ResumeReport(
        reattached=tuple(reattached),
        timed_out=tuple(timed_out),
        orphaned=tuple(orphaned),
    )
