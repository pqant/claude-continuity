"""lock.py: fcntl.flock project lock with stale takeover (PRD §11.1, §14).

Lock file content: {"pid": …, "ts": …, "rotation_id": …}. Stale = older than
120 s AND recorded PID dead → taken over (recreate the file). A fresh live
holder is never taken over.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from cc_continuity import lock


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_pid_alive_current_process():
    assert lock.pid_alive(os.getpid()) is True


def test_pid_alive_dead_process():
    assert lock.pid_alive(_dead_pid()) is False


def test_pid_alive_non_positive_is_dead():
    assert lock.pid_alive(0) is False
    assert lock.pid_alive(-3) is False


def test_is_stale_requires_old_ts_and_dead_pid():
    now = time.time()
    # Unreadable content: cannot prove staleness → never stale (wait/timeout).
    assert lock.is_stale_info(None) is False
    # The spec rule (PRD §11.1): old AND dead.
    assert lock.is_stale_info({"pid": os.getpid(), "ts": now}) is False
    assert lock.is_stale_info({"pid": _dead_pid(), "ts": now - 300}) is True
    assert lock.is_stale_info({"pid": _dead_pid(), "ts": now}) is False
    assert lock.is_stale_info({"pid": os.getpid(), "ts": now - 300}) is False
    assert lock.is_stale_info({"pid": "garbage", "ts": now - 300}) is False


def test_acquire_release_and_contention(tmp_path):
    p = tmp_path / "lock"
    a, b = lock.ProjectLock(p), lock.ProjectLock(p)
    assert a.acquire() is True
    assert b.acquire(timeout=0) is False  # held by a
    a.release()
    assert b.acquire(timeout=0) is True
    b.release()


def test_acquire_waits_then_succeeds_after_release(tmp_path):
    p = tmp_path / "lock"
    a, b = lock.ProjectLock(p), lock.ProjectLock(p)
    assert a.acquire() is True
    events = []

    def holder():
        time.sleep(0.3)
        a.release()
        events.append("released")

    t = threading.Thread(target=holder)
    t.start()
    assert b.acquire(timeout=5) is True
    events.append("acquired")
    t.join()
    b.release()
    assert events == ["released", "acquired"]


def test_stale_lock_is_taken_over(tmp_path):
    p = tmp_path / "lock"
    a, b = lock.ProjectLock(p), lock.ProjectLock(p)
    assert a.acquire() is True
    # Simulate the documented stale case: recorded holder dead, entry old.
    p.write_text(json.dumps({"pid": _dead_pid(), "ts": time.time() - 300, "rotation_id": None}))
    assert b.acquire(timeout=2) is True  # takeover, not a timeout
    info = json.loads(p.read_text())
    assert info["pid"] == os.getpid()
    assert "ts" in info and "rotation_id" in info
    b.release()


def test_fresh_live_holder_not_taken_over(tmp_path):
    p = tmp_path / "lock"
    a, b = lock.ProjectLock(p), lock.ProjectLock(p)
    assert a.acquire() is True
    assert b.acquire(timeout=0.2) is False
    # Still held, content untouched.
    assert lock.ProjectLock(p).acquire(timeout=0) is False
    a.release()


def test_context_manager_releases(tmp_path):
    p = tmp_path / "lock"
    with lock.ProjectLock(p):
        assert lock.ProjectLock(p).acquire(timeout=0) is False
    assert lock.ProjectLock(p).acquire(timeout=0) is True


def test_context_manager_raises_when_held(tmp_path):
    p = tmp_path / "lock"
    a = lock.ProjectLock(p)
    assert a.acquire() is True
    with pytest.raises(lock.LockHeldError):
        with lock.ProjectLock(p, timeout=0.2):
            pass
    a.release()


def test_rotation_id_recorded(tmp_path):
    p = tmp_path / "lock"
    l = lock.ProjectLock(p)
    assert l.acquire(rotation_id="rot-123") is True
    assert json.loads(p.read_text())["rotation_id"] == "rot-123"
    l.release()
