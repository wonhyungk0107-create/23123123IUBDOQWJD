"""Injectable clock.

Time is a dependency. Quote staleness, trade-lock expiry, capital-days and
reservation timeouts are all economic quantities derived from "now", so tests and
the offline demo must be able to control it exactly -- otherwise the demo's output
changes every run and G12 (reproducibility) is untestable.

Nothing outside this module may call :func:`datetime.now`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Source of the current UTC time."""

    def now(self) -> datetime:
        """Timezone-aware UTC. Never naive."""
        ...


class SystemClock:
    """Real wall clock. The only place ``datetime.now`` is permitted."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """Deterministic clock for tests, fixtures and the offline demo.

    Advances only when told to, so a scan pipeline can simulate elapsed time
    (a quote ageing past its limit, a trade lock expiring) without sleeping.
    """

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> datetime:
        """Move forward. Refuses to go backwards -- that would corrupt ordering."""
        if delta < timedelta(0):
            raise ValueError("FixedClock cannot move backwards")
        self._instant = self._instant + delta
        return self._instant

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant")
        if instant < self._instant:
            raise ValueError("FixedClock cannot move backwards")
        self._instant = instant.astimezone(UTC)

    def __repr__(self) -> str:
        return f"FixedClock({self._instant.isoformat()})"
