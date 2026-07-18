"""State-transition events emitted by the runners.

Defined in ``jobs/`` rather than ``web/`` because the dependency direction is
one-way: ``web`` imports ``jobs``, never the reverse (spec section 4.1). The
web layer re-exports ``JobEvent`` and supplies the fan-out bus that implements
``EventPublisher``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from higgshole.store.db import ErrorReason, GenerationKind, GenerationState


@dataclass(frozen=True)
class JobEvent:
    """One state transition, broadcast to every listener."""

    generation_id: str
    kind: GenerationKind
    state: GenerationState
    error_reason: ErrorReason | None
    detail: str | None
    at: str

    def to_sse(self) -> str:
        """Render as a single Server-Sent Events frame."""
        payload = json.dumps(
            {
                "generation_id": self.generation_id,
                "kind": str(self.kind),
                "state": str(self.state),
                "error_reason": (
                    None if self.error_reason is None else str(self.error_reason)
                ),
                "detail": self.detail,
                "at": self.at,
            },
            sort_keys=True,
        )
        return f"event: job\ndata: {payload}\n\n"


@runtime_checkable
class EventPublisher(Protocol):
    """Anything the runners can hand a JobEvent to.

    Publishing must never block or raise: a slow browser tab must not stall a
    job runner, and a failed broadcast must not fail a paid generation.
    """

    def publish(self, event: JobEvent) -> None: ...


class NullEventPublisher:
    """Discards every event. The default when no bus has been wired."""

    def publish(self, event: JobEvent) -> None:
        return None
