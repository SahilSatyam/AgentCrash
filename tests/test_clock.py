"""VirtualClock behavior: tick default, t=0 start, now/sleep/deadline."""

import pytest

from agentcrash.clock import VirtualClock


def test_defaults_tick_50_and_t_zero() -> None:
    clock = VirtualClock()
    assert clock.tick_ms == 50
    assert clock.now() == 0


def test_sleep_advances_and_returns_new_time() -> None:
    clock = VirtualClock()
    assert clock.sleep(300) == 300
    assert clock.now() == 300
    assert clock.sleep(120) == 420


def test_deadline_does_not_advance_clock() -> None:
    clock = VirtualClock()
    assert clock.deadline(1500) == 1500
    assert clock.now() == 0


def test_negative_inputs_rejected() -> None:
    clock = VirtualClock()
    with pytest.raises(ValueError):
        VirtualClock(tick_ms=0)
    with pytest.raises(ValueError):
        VirtualClock(t=-1)
    with pytest.raises(ValueError):
        clock.sleep(-5)
    with pytest.raises(ValueError):
        clock.deadline(-5)
