"""watchdog.py: Phase 10 pure decision core (spec P10-D4/D6/D7).

Zero I/O — every input is an argument, every output a value.
"""

import dataclasses

from cc_continuity import watchdog


# --- HealthState (P10-D4: 2-fail debounce, cold start UNKNOWN) --------------

def test_health_cold_start_first_failure_is_failure_1():
    h = watchdog.HealthState()
    assert h.status == "UNKNOWN"
    assert h.feed(ok=False, now=100.0) is None  # failure 1: no transition yet
    assert h.status == "UNKNOWN"
    assert h.consecutive_fails == 1


def test_health_down_after_two_consecutive_failures():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    assert h.feed(ok=False, now=400.0) == "down"
    assert h.status == "DOWN"
    assert h.down_since == 400.0


def test_health_one_fail_then_ok_stays_unknown():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    assert h.feed(ok=True, now=400.0) is None  # UNKNOWN → UP: never announced
    assert h.status == "UP"
    assert h.consecutive_fails == 0
    assert h.down_since is None


def test_health_up_to_down_transition():
    h = watchdog.HealthState()
    h.feed(ok=True, now=100.0)
    assert h.status == "UP"
    h.feed(ok=False, now=400.0)
    assert h.feed(ok=False, now=700.0) == "down"
    assert h.down_since == 700.0


def test_health_down_to_up_transition():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    h.feed(ok=False, now=400.0)          # DOWN
    assert h.feed(ok=True, now=2000.0) == "up"
    assert h.status == "UP"
    assert h.down_since is None  # consumed by the daemon for the push body
