"""rotator.py: PRD §11 full-auto rotator (fake tmux per PRD §21).

The fake tmux records argv (one arg per line, "---" between calls) and can
run side-effect hook commands — Tasks 7–10 use it to simulate the pane.
"""

import json
import os
import subprocess
import time

import pytest

from cc_continuity import bootstrap, ledger, lock, paths, rotator, state

from hookutil import (SID, make_repo, register_session, run_hook,
                      set_metric, walk_to)

FAKE_TMUX = """#!/bin/sh
printf '%s\\n' "---" "$@" >> "${FAKE_TMUX_LOG:?}"
case "$*" in
  *' /clear'*) if [ -n "${FAKE_CLEAR_HOOK:-}" ]; then eval "$FAKE_CLEAR_HOOK"; fi ;;
  *paste-buffer*) if [ -n "${FAKE_PASTE_HOOK:-}" ]; then eval "$FAKE_PASTE_HOOK"; fi ;;
esac
exit 0
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(FAKE_TMUX)
    os.chmod(bin_path, 0o755)
    log = tmp_path / "tmux-args.log"
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    return log


def _calls(log) -> list[list[str]]:
    """Parse the fake-tmux log into a list of argv lists."""
    out, cur = [], None
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            if cur is not None:
                out.append(cur)
            cur = []
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        out.append(cur)
    return out


def _quiet_session(cc_home, tmp_path) -> tuple:
    """Registered session, turn closed: turn_active False + last_stop_ts set."""
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    return repo, key, nnn


# --- tmux helper -------------------------------------------------------------


def test_tmux_helper_uses_env_bin_and_records(fake_tmux):
    assert rotator._tmux("send-keys", "-t", "%9", "-l", "/clear") == 0
    assert _calls(fake_tmux) == [["send-keys", "-t", "%9", "-l", "/clear"]]


def test_tmux_helper_missing_bin_returns_1(cc_home, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", "/nonexistent/tmux")
    assert rotator._tmux("list-sessions") == 1


# --- idle check (PRD §11.3) ---------------------------------------------------


def test_idle_true_when_quiet(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    assert rotator._idle(key, nnn) is True


def test_idle_false_when_turn_active(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    state.update_fields(key, nnn, turn_active=True)
    assert rotator._idle(key, nnn) is False


def test_idle_false_on_main_entry_after_stop(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "user_prompt", "text": "user typed something"})
    assert rotator._idle(key, nnn) is False


def test_idle_ignores_subagent_entries_after_stop(cc_home, tmp_path):
    # P6-D6: a background subagent finishing after the Stop is NOT
    # main-session activity.
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "task_completed", "agent_id": "agent-3",
                   "subject": "background job"})
    assert rotator._idle(key, nnn) is True


def test_idle_ignores_state_bookkeeping_after_stop(cc_home, tmp_path):
    # The Stop hook's own CV->ROTATING transition writes a "state" ledger
    # entry AFTER last_stop_ts — it must not look like activity.
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    state.transition(key, nnn, "ROTATION_PENDING")
    assert rotator._idle(key, nnn) is True


# --- .started marker wait (PRD §11.6) -----------------------------------------


def test_wait_new_started_detects_new_marker(cc_home, monkeypatch):
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    before = rotator._started_names()
    bootstrap.write_started("sid-marker")
    assert rotator._wait_new_started(before) is True


def test_wait_new_started_times_out(cc_home, monkeypatch):
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.1)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    assert rotator._wait_new_started(rotator._started_names()) is False


# --- spawn (P6-D8) -------------------------------------------------------------


def test_spawn_detached_with_cli_command(cc_home, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))
            self.pid = 4242

    monkeypatch.setattr(rotator.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(rotator.shutil, "which",
                        lambda name: "/usr/bin/cc-continuity")
    pid = rotator.spawn("sid-1", "%7", "rid-1")
    assert pid == 4242
    cmd, kw = calls[0]
    assert cmd == ["/usr/bin/cc-continuity", "rotate", "--session", "sid-1",
                   "--pane", "%7", "--rotation-id", "rid-1"]
    assert kw["start_new_session"] is True
    assert kw["stdin"] is subprocess.DEVNULL
    assert "rotator.log" in str(kw["stdout"].name)


def test_command_falls_back_to_python_module(cc_home, monkeypatch):
    monkeypatch.setattr(rotator.shutil, "which", lambda name: None)
    assert rotator._command() == [rotator.sys.executable,
                                  "-m", "cc_continuity"]


def test_spawn_raises_when_popen_fails(cc_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(rotator.subprocess, "Popen", boom)
    with pytest.raises(rotator.RotatorSpawnError):
        rotator.spawn("sid", "%1", "r")
