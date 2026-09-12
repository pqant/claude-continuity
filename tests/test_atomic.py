"""atomic.py: race-safe writes (temp + fsync + os.replace).

Hooks run concurrently (one model message → many parallel tool calls, S1/N1),
so every file write must be indistinguishable from a single instantaneous change.
"""

import json
import os
import threading

from cc_continuity import atomic


def test_write_text_creates_exact_content(tmp_path):
    target = tmp_path / "out.txt"
    atomic.atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_write_json_roundtrip(tmp_path):
    target = tmp_path / "out.json"
    atomic.atomic_write_json(target, {"a": 1, "b": [1, 2]})
    assert json.loads(target.read_text()) == {"a": 1, "b": [1, 2]}


def test_overwrite_replaces_previous_content(tmp_path):
    target = tmp_path / "out.txt"
    atomic.atomic_write_text(target, "first")
    atomic.atomic_write_text(target, "second")
    assert target.read_text() == "second"


def test_no_temp_files_left_on_success(tmp_path):
    target = tmp_path / "out.txt"
    atomic.atomic_write_text(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_replace_failure_leaves_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic.atomic_write_text(target, "original")

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    try:
        atomic.atomic_write_text(target, "new content that must not land")
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError from patched os.replace")
    assert target.read_text() == "original"  # old value survives the "crash"


def test_concurrent_writers_never_tear(tmp_path):
    target = tmp_path / "hot.txt"
    n_threads, n_iters = 8, 40
    values = set()

    def worker(tid):
        for i in range(n_iters):
            value = f"thread-{tid}-iter-{i} " + "z" * 2000  # > one page
            values.add(value)
            atomic.atomic_write_text(target, value)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Final content must be exactly one complete write — never a torn mix.
    assert target.read_text() in values
