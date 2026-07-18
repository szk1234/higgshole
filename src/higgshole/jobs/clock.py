"""Injectable time.

Every wait in the job engine goes through a Clock so that the wall-clock
ceiling (spec section 4.3) and the retry backoff (spec section 4.4) can be
tested exhaustively without a test ever sleeping.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never affected by clock changes."""

    async def sleep(self, seconds: float) -> None:
        """Yield to the event loop for a duration."""


class RealClock:
    """The production clock. Monotonic, so an NTP step cannot un-expire a job."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
