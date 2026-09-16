"""Full-auto rotator (PRD §11). Implemented in Phase 6.

Spawned detached by the Stop hook: project rotation lock (flock, stale
takeover) → re-read state (must be ROTATING with the same rotation_id) →
idle check (settle_ms, turn_active, no ledger activity) → ``C-u`` →
``send-keys /clear`` + Enter → wait for ``runtime/<sid>.started`` →
``/rename <slug>-<NNN+1>`` → continuation prompt via
``set-buffer``/``paste-buffer -p -d`` → verify ``CONTINUITY RESUME`` in the
new session's ledger within 15 s → ROTATION_COMPLETED or (retry once)
ROTATION_FAILED + notify. Never kills Claude Code, never deletes
checkpoints.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import ledger, lock, naming, paths, state, thresholds
from .hooks.common import log_event

#: PRD §11 wait windows (seconds). Module constants so tests can shrink
#: them via monkeypatch; polling granularity shared by both waits.
STARTED_WAIT_S = 10.0    # §11.6 — SessionStart(clear) .started marker
VERIFY_WAIT_S = 15.0     # §11.9 — RESUME prompt in the successor ledger
POLL_S = 0.25
TMUX_BUFFER = "ccc"      # §11.8 paste-buffer name


class RotatorSpawnError(RuntimeError):
    """The detached rotator process could not be started (P6-D8)."""


# --- tmux (PRD §21: CC_CONTINUITY_TMUX_BIN override) --------------------------


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def _tmux(*args: str) -> int:
    """Run one tmux command; returncode, or 1 when it cannot be spawned."""
    try:
        proc = subprocess.run([_tmux_bin(), *args], capture_output=True,
                              timeout=10)
        return proc.returncode
    except (OSError, subprocess.SubprocessError) as e:
        log_event(f"rotator: tmux call failed: {e}")
        return 1


# --- spawn (P6-D8, called by the Stop hook) ------------------------------------


def _command() -> list[str]:
    exe = shutil.which("cc-continuity")
    return [exe] if exe else [sys.executable, "-m", "cc_continuity"]


def spawn(session_id: str, pane: str, rotation_id: str) -> int:
    """PRD §9 Stop: spawn the rotator detached; returns the child PID.

    Double-detached via ``start_new_session`` (setsid) so the rotator
    outlives the hook process; stdout/stderr append to ``logs/rotator.log``.
    Raises :class:`RotatorSpawnError` when the process cannot start.
    """
    cmd = _command() + ["rotate", "--session", session_id, "--pane", pane,
                        "--rotation-id", rotation_id]
    log_path = paths.logs_dir() / "rotator.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as logf:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=logf,
                                    start_new_session=True)
    except OSError as e:
        raise RotatorSpawnError(f"rotator spawn failed: {e}") from e
    return proc.pid


# --- idle check (PRD §11.3) -----------------------------------------------------


def _idle(key: str, nnn: int) -> bool:
    """PRD §11.3: turn inactive AND no main-session ledger activity since
    the last Stop. Entries with ``agent_id`` are subagent work and do NOT
    defer the rotation (P6-D6). ``type == "state"`` entries are our own
    transition bookkeeping (the Stop hook's CV→ROTATING move lands AFTER
    ``last_stop_ts``) and must not count as activity either."""
    rec = state.load_state(key, nnn)
    if rec.get("turn_active"):
        return False
    last_stop = rec.get("last_stop_ts") or 0
    for entry in ledger.read(paths.session_dir(key, nnn)):
        if entry.get("agent_id") or entry.get("type") == "state":
            continue
        if entry.get("ts", 0) > last_stop:
            return False
    return True


# --- .started marker (PRD §11.6) -------------------------------------------------


def _started_names() -> set:
    d = paths.runtime_dir()
    return {p.name for p in d.glob("*.started")} if d.is_dir() else set()


def _wait_new_started(before: set) -> bool:
    """True when a ``runtime/*.started`` file appears that was not in
    ``before`` (captured before the /clear keystrokes)."""
    deadline = time.monotonic() + STARTED_WAIT_S
    while time.monotonic() < deadline:
        if _started_names() - before:
            return True
        time.sleep(POLL_S)
    return False
