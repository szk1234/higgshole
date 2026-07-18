"""The two generation state machines (spec section 4.3).

Image and video have different shapes and deliberately do not share a machine:

    image:  PENDING -> GENERATING -> WRITING -> COMPLETE
    video:  PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE

with REJECTED and FAILED branches on both. Only video rows can ever occupy
SUBMITTED, RUNNING or DOWNLOADING, which is what makes boot-time reattachment
(jobs/resume.py) safe to scope by state.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from higgshole.store.db import (
    ErrorReason,
    GenerationKind,
    GenerationState,
    InputRole,
)


@dataclass(frozen=True)
class GenerationRequest:
    """A validated, project-resolved request.

    Built by web/api.py. The runner never parses HTTP input, so the same
    engine serves the REST API and any future caller unchanged.
    """

    kind: GenerationKind
    project_id: str
    project_slug: str
    model: str
    prompt: str
    params: dict[str, Any]
    #: (asset_id, role) pairs in the order the operator supplied them.
    inputs: tuple[tuple[str, InputRole], ...] = ()


@dataclass(frozen=True)
class GenerationOutcome:
    """What a runner returns to its caller."""

    generation_id: str
    state: GenerationState
    file_path: str | None
    asset_id: str | None
    cost: Decimal | None
    error_reason: ErrorReason | None
    error_detail: str | None


@dataclass(frozen=True)
class RetryPolicy:
    """Spec section 4.4.

    Submission is never blindly retried: POST /images is synchronous and
    non-idempotent, so a retry risks a second charge. Only 429-before-dispatch
    and idempotent GETs (poll, download) use this.
    """

    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at max_delay_s.

        Full jitter rather than a fixed schedule because several pollers may
        back off from the same 429 at the same instant; sampling from
        [0, ceiling] de-synchronises them.
        """
        exponent = max(0, attempt)
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**exponent))
        return random.uniform(0.0, ceiling)


#: Provider job status -> (internal state, error reason). Spec section 4.3.
_STATUS_MAP: dict[str, tuple[GenerationState, ErrorReason | None]] = {
    "pending": (GenerationState.RUNNING, None),
    "in_progress": (GenerationState.RUNNING, None),
    "completed": (GenerationState.DOWNLOADING, None),
    "failed": (GenerationState.FAILED, ErrorReason.PROVIDER_FAILED),
    "cancelled": (GenerationState.FAILED, ErrorReason.PROVIDER_CANCELLED),
    "expired": (GenerationState.FAILED, ErrorReason.PROVIDER_EXPIRED),
}


def map_provider_status(status: str) -> tuple[GenerationState, ErrorReason | None]:
    """Provider job status -> (internal state, error reason).

    Unrecognised statuses map to (RUNNING, None): over-polling is bounded by
    the wall-clock ceiling and self-corrects, while treating a live job as
    terminal loses a paid generation irrecoverably (spec section 2.4).
    """
    return _STATUS_MAP.get(status, (GenerationState.RUNNING, None))
