"""Full-auto rotator (PRD §11). Implemented in Phase 6.

Spawned detached by the Stop hook: project rotation lock (flock, stale
takeover) → re-read state (must be ROTATING with the same rotation_id) →
idle check (settle_ms, turn_active, no ledger activity) → input-box
protection: capture the pane, save any in-progress prompt to
``runtime/pending-prompt-<key>-<NNN>.txt``, ``C-u``, wait for the box to
settle EMPTY (defer when the user is still typing) →
``send-keys /clear`` + Enter → wait for ``runtime/<sid>.started`` →
``/rename <slug>-<NNN+1>`` → continuation prompt via
``set-buffer``/``paste-buffer -p -d`` (with a reference-only note carrying
the saved in-progress prompt when there was one) → verify
``CONTINUITY RESUME`` in the new session's ledger within 15 s →
ROTATION_COMPLETED or (retry once) ROTATION_FAILED + notify. Never kills
Claude Code, never deletes checkpoints.

Live bug fixed here (session 039, 2026-09-26): the old ``C-u → /clear →
Enter`` fired while the user was still typing, so ``/clear`` got appended
to their in-progress prompt and submitted as one garbage prompt — the
rotation failed and the user's message was mangled. ``C-u`` only clears
what was typed BEFORE it; the fix captures the text first, saves it, and
never sends ``/clear`` unless the box is verified settled-empty.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import (atomic, ledger, lock, naming, notify, paths, state,
               statusline, thresholds, watchdog)
from .hooks.common import log_event

#: PRD §11 wait windows (seconds). Module constants so tests can shrink
#: them via monkeypatch; polling granularity shared by both waits.
STARTED_WAIT_S = 10.0    # §11.6 — SessionStart(clear) .started marker
VERIFY_WAIT_S = 15.0     # §11.9 — RESUME prompt in the successor ledger
POLL_S = 0.25
TMUX_BUFFER = "ccc"      # §11.8 paste-buffer name

#: Input-box protection: how long to wait for the box to settle EMPTY
#: after C-u, and how many consecutive empty captures "settled" requires.
CLEAR_SETTLE_S = 2.0
SETTLE_POLLS = 2

#: Paneless self-heal: how long to wait for the successor's claim
#: (SessionStart(startup) on a ROTATING project closes the old session)
#: before deferring to the next Stop.
CLAIM_WAIT_S = 20.0

#: Cap for the pending-prompt note appended to the C.8 continuation prompt.
PENDING_NOTE_CAP = 800



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


def spawn_successor_detached(key: str, nnn: int, session_id: str, cfg: dict,
                             rotation_id: str, *,
                             verify=None,
                             sleep=time.sleep) -> tuple[str, dict | None]:
    """Paneless self-heal — the Stop-hook fallback when ``$TMUX_PANE`` is
    missing (the session was launched outside tmux, so the pane-driven
    rotator cannot work; rotation must still work in every situation).

    Spawns the successor as a fresh claude in a DETACHED tmux session
    carrying the C.8 continuation prompt. The successor's
    SessionStart(startup) claims the rotation on a ROTATING project
    (bootstrap closes the old session), and the C.8 prompt is verified in
    its ledger. The successor is inside a tmux pane from its first
    heartbeat: auto-rotation and the animated progress pane work.

    Returns ``(outcome, info)``:
      ``("completed", {"name", "tmux_session"})`` — claim + RESUME
          verified; rotation_completed notified.
      ``("deferred", {"name", "tmux_session"})`` — no claim in time (rolled
          back to CHECKPOINT_VERIFIED; the next Stop retries) or the RESUME
          never reached the successor ledger (the old session is already
          terminal ROTATED; error notified).
      ``("failed", None)`` — the tmux session could not be created (rolled
          back to CHECKPOINT_VERIFIED; error notified).
      ``("exited", None)`` — rotation lock held by another rotator, or
          state/rotation_id mismatch: nothing was done.
    """
    from .launch import _claude_bin, _env_prefix  # local import: light module
    if verify is None:  # late bind: _verify_resume is defined below
        verify = _verify_resume

    rlock = lock.ProjectLock(paths.project_dir(key) / "rotation.lock",
                             timeout=0)
    if not rlock.acquire(rotation_id=rotation_id):
        log_event(f"rotator: paneless spawn: rotation lock held key={key} "
                  "— exiting (another rotator is live)")
        return "exited", None
    try:
        # Same §11.2 re-read as run(): must be ROTATING, same rotation_id.
        rec = state.load_state(key, nnn)
        if rec["state"] != "ROTATING" or rec.get("rotation_id") != rotation_id:
            log_event(f"rotator: paneless spawn: state {rec['state']!r} / "
                      f"rotation_id {rec.get('rotation_id')!r} != "
                      f"ROTATING/{rotation_id} — exiting")
            return "exited", None
        pref = json.loads((paths.project_dir(key) / "project.json")
                          .read_text(encoding="utf-8"))
        root = Path(pref["root"])
        name_new = _successor_name(key, nnn, rec["name"])
        c8 = thresholds.c8_text(f"cp-{nnn:03d}", _handoff_path(key, nnn))
        args = ["-n", name_new]
        if bool(cfg.get("unattended", False)):
            args.append("--dangerously-skip-permissions")
        args.append(c8)
        cmd = " ".join(["env", *_env_prefix(name_new), "command",
                        _claude_bin(), *[shlex.quote(a) for a in args]])
        tmux_session = (f"cc-{naming.slug_for_dir(root)}-"
                        f"{secrets.token_hex(2)}")
        rc = _tmux("new-session", "-d", "-s", tmux_session, "-c",
                   str(root), cmd)
        info = {"name": name_new, "tmux_session": tmux_session}
        if rc != 0:
            _defer_to_checkpoint(key, nnn,
                                 "paneless spawn: tmux new-session failed")
            notify.safe_send(cfg, "error", slug=notify.project_slug(key),
                             nnn=nnn, dedup_key=f"paneless_spawn:{rotation_id}",
                             what="paneless successor spawn",
                             detail="tmux new-session failed")
            return "failed", None
        # Wait for the claim: SessionStart(startup) on a ROTATING project
        # claims the rotation and moves the old session to terminal ROTATED.
        deadline = time.monotonic() + CLAIM_WAIT_S
        claimed = False
        while time.monotonic() < deadline:
            if state.load_state(key, nnn).get("state") == "ROTATED":
                claimed = True
                break
            sleep(POLL_S)
        if not claimed:
            _defer_to_checkpoint(key, nnn,
                                 "paneless spawn: no claim in time — next "
                                 "Stop retries")
            return "deferred", info
        old_name = rec["name"]
        if verify(key, nnn, f"cp-{nnn:03d}", old_name):
            succ = _find_successor_rec(key, old_name)
            r1 = None
            sid_new = (succ or {}).get("session_id")
            if sid_new:
                m_new = statusline.read_metric(sid_new)
                if not statusline.is_stale(m_new):
                    r1 = m_new.get("ratio")
            ledger.append(paths.session_dir(key, nnn),
                          {"type": "rotation_completed",
                           "successor": name_new})
            log_event(f"rotator: PANELESS_ROTATION_COMPLETED key={key} "
                      f"{nnn:03d} -> {name_new} (tmux {tmux_session})")
            notify.safe_send(cfg, "rotation_complete",
                             slug=notify.project_slug(key), nnn=nnn,
                             nnn_new=int(succ["nnn"]) if succ else nnn + 1,
                             r0=rec.get("ratio_at_pending"), r1=r1,
                             naa=notify.naa_snippet(_handoff_path(key, nnn)))
            return "completed", info
        # Claim landed but the C.8 prompt never reached the successor
        # ledger: the successor is alive with the C.4 bootstrap, and the
        # old session is terminal (ROTATED) — nothing to roll back.
        log_event(f"rotator: paneless spawn: no RESUME in successor ledger "
                  f"key={key} {nnn:03d}")
        notify.safe_send(cfg, "error", slug=notify.project_slug(key),
                         nnn=nnn, dedup_key=f"paneless_verify:{rotation_id}",
                         what="paneless rotation verify",
                         detail="no RESUME in successor ledger")
        return "deferred", info
    finally:
        rlock.release()


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


# --- input-box protection (live bug 039: /clear appended to typed text) ------


def _parse_input_box(pane_text: str) -> str:
    """The text the user is typing in Claude Code's input box, or "".

    The box is the bottom-most block of the pane: input lines (``❯`` at
    column 0 or 2-space indented, NBSP or space after the symbol) sit
    above a long ``─`` frame line, which sits above the status line.
    Frame lines may appear BETWEEN box lines (multi-line box). The scan
    walks up from the last frame line and stops at the first line that is
    neither a frame nor an input line — transcript content above the box
    (including transcript user lines, which share the ``❯`` format) is
    never collected. A wrapped input line longer than the pane width
    truncates the saved text at the wrap; the note is reference-only.
    """
    lines = pane_text.splitlines()
    bottom = None
    for i in range(len(lines) - 1, -1, -1):
        if watchdog.is_frame_line(lines[i]):
            bottom = i
            break
    if bottom is None:
        return ""
    out: list[str] = []
    for i in range(bottom - 1, -1, -1):
        s = lines[i]
        if watchdog.is_frame_line(s):
            continue
        m = watchdog.INPUT_LINE_RE.match(s)
        if m is None:
            break
        out.append(s[m.end():].rstrip())
    return "\n".join(reversed(out))


def capture_pane_text(pane: str) -> str:
    """``capture-pane -p`` text, or "" when the call fails."""
    try:
        proc = subprocess.run([_tmux_bin(), "capture-pane", "-t", pane, "-p"],
                              capture_output=True, timeout=10)
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def input_box_text(pane: str, *, capture=capture_pane_text) -> str:
    """In-progress input-box text for ``pane`` ("" when the box is empty)."""
    return _parse_input_box(capture(pane))


def pending_prompt_path(key: str, nnn: int) -> Path:
    return paths.runtime_dir() / f"pending-prompt-{key}-{nnn:03d}.txt"


def save_pending_prompt(key: str, nnn: int, text: str) -> None:
    atomic.atomic_write_text(pending_prompt_path(key, nnn), text)


def read_pending_prompt(key: str, nnn: int) -> str:
    p = pending_prompt_path(key, nnn)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def delete_pending_prompt(key: str, nnn: int) -> None:
    try:
        pending_prompt_path(key, nnn).unlink(missing_ok=True)
    except OSError:
        pass


def c8_with_pending(base: str, pending: str, cap: int = PENDING_NOTE_CAP,
                    ) -> str:
    """C.8 continuation prompt with a reference-only pending-prompt note.

    The user's in-progress text is shown in the new session (screen
    reminder) but is explicitly NOT an instruction — the successor
    continues from the handoff. ``base`` is returned unchanged when
    ``pending`` is empty."""
    text = pending.strip()
    if not text:
        return base
    if len(text) > cap:
        text = text[:cap].rstrip() + " …(truncated)"
    return (base + "\nPENDING USER PROMPT (reference only): the user was "
            "typing this when the rotation fired; it was NOT submitted and "
            "is NOT a new instruction. Continue from the handoff; the user "
            "may re-send it.\n> " + text.replace("\n", " ⏎ "))


def _clear_pane(pane: str, key: str, nnn: int, *,
                capture=capture_pane_text) -> bool:
    """PRD §11.4–5, hardened: save the in-progress prompt, then /clear ONLY
    when the input box is verified settled-empty.

    Live bug (session 039): the old blind ``C-u → /clear → Enter`` fired
    while the user was still typing — C-u clears only what was typed
    BEFORE it, so the user's continuing keys plus the literal ``/clear``
    were submitted as one garbage prompt. Now: capture the box and save
    the text (for the C.8 reminder), C-u, poll until the box is empty
    ``SETTLE_POLLS`` times in a row (``CLEAR_SETTLE_S`` budget), and only
    then send ``/clear`` + Enter. Returns False when the box never settles
    (the user is still typing) — the caller defers; ``/clear`` is never
    sent in that case."""
    typed = _parse_input_box(capture(pane))
    if typed:
        save_pending_prompt(key, nnn, typed)
    _tmux("send-keys", "-t", pane, "C-u")
    empty_streak = 0
    deadline = time.monotonic() + CLEAR_SETTLE_S
    while True:
        if not _parse_input_box(capture(pane)):
            empty_streak += 1
            if empty_streak >= SETTLE_POLLS:
                break
        else:
            empty_streak = 0
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    _tmux("send-keys", "-t", pane, "-l", "/clear")
    _tmux("send-keys", "-t", pane, "Enter")
    return True


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


def _defer_to_checkpoint(key: str, nnn: int, reason: str) -> None:
    """Roll ``ROTATING`` back to ``CHECKPOINT_VERIFIED`` (next Stop retries).

    Tolerates the state having advanced to ``ROTATED`` concurrently: bootstrap
    closes the old session (ROTATED) when the successor's ``SessionStart/clear``
    claims the rotation, which can land while a defer is being decided (the
    continuation prompt already launched the successor). ``ROTATED`` is
    terminal, so there is nothing to roll back — log and skip instead of
    raising (regression 2026-09-27 049/050: InvalidTransitionError
    ROTATED->CHECKPOINT_VERIFIED mid-rotation).
    """
    try:
        state.transition(key, nnn, "CHECKPOINT_VERIFIED")
    except state.InvalidTransitionError as e:
        rec = state.load_state(key, nnn)
        log_event(f"rotator: defer no-op (state advanced to "
                  f"{rec['state']!r}) key={key} nnn={nnn:03d} ({reason}): {e}")
        return
    log_event(f"rotator: ROTATION_DEFERRED key={key} nnn={nnn:03d} "
              f"({reason})")


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
            _defer_to_checkpoint(key, nnn,
                                 "activity after Stop — next Stop retries")
            return 0
        cp_id = f"cp-{nnn:03d}"
        for attempt in (1, 2):  # §11.9: retry steps 3–9 once
            # The retry is for a botched keystroke sequence, NOT a second
            # rotation. If the state already advanced to ROTATED (bootstrap
            # closed the old session when the successor's SessionStart/clear
            # claimed the rotation — the previous attempt's continuation
            # prompt already launched it), stop: re-driving the pane would
            # fight the live successor, and ROTATED is terminal.
            if state.load_state(key, nnn)["state"] == "ROTATED":
                log_event(f"rotator: rotation already completed (ROTATED) "
                          f"key={key} nnn={nnn:03d} attempt={attempt} — "
                          "not re-driving")
                return 0
            # §11.6 — remember existing markers BEFORE the keystrokes.
            before = _started_names()
            # §11.4–5 — save in-progress input, then /clear only when the
            # box is verified settled-empty; defer while the user types.
            if not _clear_pane(pane, key, nnn):
                _defer_to_checkpoint(key, nnn,
                                     "input box busy — user still typing; "
                                     "next Stop retries")
                return 0
            if not _wait_new_started(before):
                log_event("rotator: SESSIONSTART_MISSING — continuing "
                          "anyway (F14; the continuation prompt is "
                          "self-sufficient)")
            # §11.7 — rename to the successor (S10: name survives /clear).
            name_new = _successor_name(key, nnn, rec["name"])
            _tmux("send-keys", "-t", pane, "-l", f"/rename {name_new}")
            _tmux("send-keys", "-t", pane, "Enter")
            # §11.8 — continuation prompt, byte-exact via paste-buffer
            # (plus the reference-only pending-prompt note, when the user
            # was typing at rotation time).
            c8 = thresholds.c8_text(cp_id, _handoff_path(key, nnn))
            c8 = c8_with_pending(c8, read_pending_prompt(key, nnn))
            _tmux("set-buffer", "-b", TMUX_BUFFER, c8)
            _tmux("paste-buffer", "-p", "-d", "-b", TMUX_BUFFER, "-t", pane)
            _tmux("send-keys", "-t", pane, "Enter")
            # §11.9 — verify.
            if _verify_resume(key, nnn, cp_id, rec["name"]):
                state.transition(key, nnn, "ROTATED")
                delete_pending_prompt(key, nnn)  # consumed by the C.8 note
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
