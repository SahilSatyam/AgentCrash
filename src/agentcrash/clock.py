"""Virtual clock: the only time source for synthetic tools (ADR-003, ADR-006).

All time is integer virtual milliseconds. The clock starts at ``t=0`` and
advances only via :meth:`VirtualClock.sleep`. Wall-clock time is never read
here (see ``tests/test_acr001.py``).
"""

from __future__ import annotations


class VirtualClock:
    """Harness-owned virtual clock (tick 50ms, ``t=0`` at run start)."""

    def __init__(self, tick_ms: int = 50, t: int = 0) -> None:
        if tick_ms <= 0:
            raise ValueError("tick_ms must be positive")
        if t < 0:
            raise ValueError("t must be non-negative")
        self.tick_ms = tick_ms
        self._t = t

    def now(self) -> int:
        """Return the current virtual time in ms."""
        return self._t

    def sleep(self, ms: int) -> int:
        """Advance virtual time by ``ms`` and return the new time."""
        if ms < 0:
            raise ValueError("sleep ms must be non-negative")
        self._t += ms
        return self._t

    def deadline(self, ms: int) -> int:
        """Return the virtual timestamp ``ms`` after now (clock unchanged)."""
        if ms < 0:
            raise ValueError("deadline ms must be non-negative")
        return self._t + ms
