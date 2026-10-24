"""daemon_supervisor: capped-backoff respawn, crash-loop guard, digest
run-loop interval (spec §7.3).

The respawn test runs the supervisor in a daemon thread: FakeChild's
3rd ``wait()`` blocks forever ("child stays up"), so the assertions are
polled against the observable spawn/sleep sequence.
"""

import threading
import time

import pytest


class FakeChild:
    def __init__(self, exits=0):
        self._exits = exits
        self.returncode = None

    def wait(self):
        self._exits -= 1
        if self._exits < 0:
            threading.Event().wait()  # stays up: block (test ends first)
        self.returncode = 0 if self._exits >= 0 else 1

    def terminate(self):
        self.returncode = 143


def test_respawn_with_backoff(tmp_path, monkeypatch):
    from cc_continuity import daemon_supervisor as ds
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(tmp_path / ".claude-continuity"))
    sleeps, spawned = [], []
    child = FakeChild(exits=2)
    t = threading.Thread(
        target=lambda: ds.run_supervisor(
            "watchdog", tmp_path,
            spawn=lambda *a, **kw: (spawned.append(1), child)[1],
            sleep=sleeps.append, now=lambda: 0.0,
            push=lambda *a, **kw: False),
        daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and (len(spawned) < 3 or len(sleeps) < 2):
        time.sleep(0.01)
    assert sleeps == [1, 2]          # backoff starts at 1 s, then 2 s
    assert len(spawned) == 3         # initial + 2 respawns, then child stays up


def test_crashloop_giveup_without_ntfy(tmp_path, monkeypatch):
    from cc_continuity import daemon_supervisor as ds
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(tmp_path / ".claude-continuity"))
    pushed = []
    child = FakeChild(exits=99)
    rc = ds.run_supervisor("watchdog", tmp_path,
                           spawn=lambda *a, **kw: child,
                           sleep=lambda s: None, now=lambda: 0.0,
                           push=lambda event, *a, **kw: (pushed.append(event), True)[1])
    assert rc == 1
    assert pushed == ["daemon-crashloop"]


def test_run_loop_interval(tmp_path):
    import cc_continuity.watchdog_digest as wd
    runs, sleeps = [], []
    ev = threading.Event()

    def fake_sleep(s):
        sleeps.append(s)
        ev.set()  # first pass runs immediately; stop after 2 passes

    rc = wd.run_loop(interval_s=120, once=lambda: (runs.append(1), 0)[1],
                     sleep=fake_sleep, stop_event=ev)
    assert rc == 0
    assert len(runs) == 2
    assert sleeps == [120]
