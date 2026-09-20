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

from . import ledger, lock, naming, notify, paths, state, statusline, thresholds
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


# --- run(): one full rotation (PRD §11 steps 1–9) ------------------------------


def _find_successor_rec(key: str, old_name: str) -> dict | None:
    """State record of the project session whose predecessor is
    ``old_name`` (the latest such), or None (claim not run yet — F14)."""
    pref = json.loads((paths.project_dir(key) / "project.json")
                      .read_text(encoding="utf-8"))
    for s in reversed(pref.get("sessions") or []):
        try:
            rec = state.load_state(key, int(s["nnn"]))
        except (state.StateNotFoundError, ValueError, KeyError):
            continue
        if rec.get("predecessor") == old_name:
            return rec
    return None


def _successor_name(key: str, nnn: int, old_name: str) -> str:
    """The name the successor has: the claimed record when the claim already
    ran, else the naming successor of the old name (F14 path)."""
    rec = _find_successor_rec(key, old_name)
    if rec is not None:
        return rec["name"]
    return naming.successor_name(old_name)


def _handoff_path(key: str, nnn: int) -> str:
    pref = json.loads((paths.project_dir(key) / "project.json")
                      .read_text(encoding="utf-8"))
    cp = pref.get("latest_checkpoint")
    if isinstance(cp, dict) and cp.get("nnn") == nnn and cp.get("handoff_path"):
        return cp["handoff_path"]
    return str(paths.session_dir(key, nnn) / "checkpoint" / "handoff.md")


def _clear_pane(pane: str) -> None:
    """PRD §11.4–5: clear any half-typed line (S9: C-u), then /clear."""
    _tmux("send-keys", "-t", pane, "C-u")
    _tmux("send-keys", "-t", pane, "-l", "/clear")
    _tmux("send-keys", "-t", pane, "Enter")


def _verify_resume(key: str, nnn: int, cp_id: str, old_name: str) -> bool:
    """PRD §11.9 — a ``CONTINUITY RESUME <cp_id>`` prompt must land in the
    successor session's ledger within ``VERIFY_WAIT_S``."""
    prefix = f"CONTINUITY RESUME {cp_id}"
    deadline = time.monotonic() + VERIFY_WAIT_S
    while time.monotonic() < deadline:
        succ = _find_successor_rec(key, old_name)
        if succ is not None:
            for entry in ledger.read(paths.session_dir(key, int(succ["nnn"]))):
                if (entry.get("type") == "user_prompt"
                        and str(entry.get("text", "")).startswith(prefix)):
                    return True
        time.sleep(POLL_S)
    return False


def _notify(cfg: dict, slug: str, event: str) -> None:
    """PRD §11.9/§19 — optional notify_command, project name + state only."""
    cmd = cfg.get("notify_command")
    if not cmd:
        return
    try:
        subprocess.run(f"{cmd} {shlex.quote(slug)} {shlex.quote(event)}",
                       shell=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log_event(f"rotator: notify failed: {e}")


def run(*, key: str, nnn: int, session_id: str, pane: str,
        rotation_id: str, cfg: dict) -> int:
    """PRD §11 — one full-auto rotation. Never kills Claude Code, never
    deletes checkpoints.

    Returns: 0 completed or deferred (idle abort — the next Stop retries),
    1 failed (``ROTATION_FAILED`` path, PRD §11.9), 2 idempotent exit
    (rotation lock held by a live rotator, or state/rotation_id mismatch).
    """
    rlock = lock.ProjectLock(paths.project_dir(key) / "rotation.lock",
                             timeout=0)
    if not rlock.acquire(rotation_id=rotation_id):
        log_event(f"rotator: ROTATION_LOCK_HELD key={key} — exiting "
                  "(another rotator is live)")
        return 2
    try:
        # §11.2 — re-read: must be ROTATING with the same rotation_id.
        rec = state.load_state(key, nnn)
        if rec["state"] != "ROTATING" or rec.get("rotation_id") != rotation_id:
            log_event(f"rotator: state {rec['state']!r} / rotation_id "
                      f"{rec.get('rotation_id')!r} != ROTATING/{rotation_id} "
                      f"— exiting")
            return 2
        pref = json.loads((paths.project_dir(key) / "project.json")
                          .read_text(encoding="utf-8"))
        # §11.3 — settle, then idle check.
        time.sleep(float(cfg.get("settle_ms", 1500)) / 1000.0)
        if not _idle(key, nnn):
            state.transition(key, nnn, "CHECKPOINT_VERIFIED")
            log_event(f"rotator: ROTATION_DEFERRED key={key} nnn={nnn:03d} "
                      "(activity after Stop — next Stop retries)")
            return 0
        cp_id = f"cp-{nnn:03d}"
        for attempt in (1, 2):  # §11.9: retry steps 3–9 once
            # §11.6 — remember existing markers BEFORE the keystrokes.
            before = _started_names()
            # §11.4–5 — clear half-typed input, then /clear.
            _clear_pane(pane)
            if not _wait_new_started(before):
                log_event("rotator: SESSIONSTART_MISSING — continuing "
                          "anyway (F14; the continuation prompt is "
                          "self-sufficient)")
            # §11.7 — rename to the successor (S10: name survives /clear).
            name_new = _successor_name(key, nnn, rec["name"])
            _tmux("send-keys", "-t", pane, "-l", f"/rename {name_new}")
            _tmux("send-keys", "-t", pane, "Enter")
            # §11.8 — continuation prompt, byte-exact via paste-buffer.
            _tmux("set-buffer", "-b", TMUX_BUFFER,
                  thresholds.c8_text(cp_id, _handoff_path(key, nnn)))
            _tmux("paste-buffer", "-p", "-d", "-b", TMUX_BUFFER, "-t", pane)
            _tmux("send-keys", "-t", pane, "Enter")
            # §11.9 — verify.
            if _verify_resume(key, nnn, cp_id, rec["name"]):
                state.transition(key, nnn, "ROTATED")
                ledger.append(paths.session_dir(key, nnn),
                              {"type": "rotation_completed",
                               "successor": name_new})
                log_event(f"rotator: ROTATION_COMPLETED key={key} {nnn:03d} "
                          f"-> {name_new}")
                succ = _find_successor_rec(key, rec["name"])
                r1 = None
                sid_new = (succ or {}).get("session_id")
                if sid_new:
                    m_new = statusline.read_metric(sid_new)
                    if not statusline.is_stale(m_new):
                        r1 = m_new.get("ratio")
                # Phase 9 — spec §5 row 5 (high priority)
                notify.safe_send(cfg, "rotation_complete",
                                 slug=notify.project_slug(key), nnn=nnn,
                                 nnn_new=int(succ["nnn"]) if succ
                                 else nnn + 1,
                                 r0=rec.get("ratio_at_pending"),
                                 r1=r1,
                                 naa=notify.naa_snippet(
                                     _handoff_path(key, nnn)))
                return 0
            log_event(f"rotator: attempt {attempt}: no RESUME in successor "
                      f"ledger — " + ("retrying" if attempt == 1
                                      else "giving up"))
        # §11.9 / P6-D7 — final failure: stay claimable, notify, exit 1.
        state.transition(key, nnn, "CHECKPOINT_VERIFIED")
        ledger.append(paths.session_dir(key, nnn),
                      {"type": "rotation_failed"})
        log_event(f"rotator: ROTATION_FAILED key={key} nnn={nnn:03d}")
        _notify(cfg, pref.get("slug", ""), "ROTATION_FAILED")
        # Phase 9 — spec §5 row 8 (the legacy notify_command call above
        # stays; both mechanisms can be active)
        notify.safe_send(cfg, "error",
                         slug=notify.project_slug(key), nnn=nnn,
                         dedup_key=f"rotation_failed:{rotation_id}",
                         what="rotation",
                         detail="no RESUME in successor ledger after 2 "
                                "attempts")
        return 1
    finally:
        rlock.release()
