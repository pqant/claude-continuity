"""SessionEnd hook (PRD §9).

Ledger entry with reason; if reason == ``clear`` while state is ``ROTATING``,
log it to ``events.log`` (successor confirmation happens at SessionStart,
Phase 6). If reason == ``resume``, the startup's process was abandoned
when it resumed a different conversation (relaunch with --continue): mark
the session ``off`` so it does not linger as a live zombie in scans
(regression 2026-09-27 047).

Phase 30: the ledger entry is always appended, but the PUSH goes through
the end-churn guard (``_end_push_allowed``) — a third-party app that
re-spawns ``claude`` in the same project every minute used to push once
a minute, indefinitely (live: ClaudeBar probe, 89 pushes / ~90 min).
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from .. import atomic, ledger, notify, paths, state
from .common import HookContext, log_error, log_event

#: End-churn guard (Phase 30): >=3 session_end entries in the last
#: SESSION_END_PUSH_STORM_WINDOW_S open a SESSION_END_PUSH_COOLDOWN_S
#: suppression window for the push (the ledger entry is never touched).
SESSION_END_PUSH_STORM_WINDOW_S = 10 * 60
SESSION_END_PUSH_STORM_THRESHOLD = 3
SESSION_END_PUSH_COOLDOWN_S = 30 * 60


def handle(ctx: HookContext) -> dict | None:
    reason = ctx.payload.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "other"
    if ctx.session is None:
        return None
    ledger.append(ctx.session_dir, {"type": "session_end", "reason": reason})
    if _end_push_allowed(ctx):
        notify.safe_send(ctx.cfg, "session_end",
                         slug=notify.project_slug(ctx.session["project_key"]),
                         nnn=int(ctx.session["nnn"]), reason=reason)
    if reason == "clear" and ctx.session.get("state") == "ROTATING":
        log_event(
            f"SessionEnd reason=clear while state=ROTATING "
            f"(project {ctx.session['project_key']}, session {ctx.session['nnn']:03d})"
        )
    if reason == "resume":
        # regression 2026-09-27 047: this startup's process was abandoned
        # the moment it resumed a different conversation (relaunch with
        # --continue registers a new sid first). Switch it off so it never
        # lingers as a live zombie in watchdog/digest scans — off flag
        # only, no state transition, no extra notify.
        try:
            state.update_fields(ctx.session["project_key"],
                                int(ctx.session["nnn"]), off=True)
        except Exception:
            log_error("SessionEnd(resume): marking session off failed")
    _cleanup_progress_pane()
    return None


def _end_push_allowed(ctx: HookContext) -> bool:
    """The Phase 30 end-churn guard; decides only the PUSH, never the
    ledger append (audit data). Never raises — any error fails OPEN
    (the push goes).

    With >=``SESSION_END_PUSH_STORM_THRESHOLD`` ``session_end`` entries
    in this session's last 10 minutes, the first such end opens a
    suppression window (one event-log line, recorded in the project's
    ``push-suppress.json``); ends inside the cooldown are silent; when
    the cooldown elapses the next end pushes again and reopens the
    window — a continuing storm stays visible at 1 push / cooldown, not
    1 / minute. A missing or corrupt state file starts a fresh window.
    """
    try:
        now = time.time()
        ends = [e for e in ledger.read(ctx.session_dir)
                if e.get("type") == "session_end"
                and isinstance(e.get("ts"), (int, float))
                and e["ts"] >= now - SESSION_END_PUSH_STORM_WINDOW_S]
        if len(ends) < SESSION_END_PUSH_STORM_THRESHOLD:
            return True
        st = (paths.project_dir(ctx.session["project_key"])
              / "push-suppress.json")
        rearm = False
        try:
            last = json.loads(st.read_text(encoding="utf-8"))["ts"]
            rearm = (isinstance(last, (int, float))
                     and now - last >= SESSION_END_PUSH_COOLDOWN_S)
        except (OSError, ValueError, KeyError):
            pass  # missing (first window) or corrupt -> fresh window
        atomic.atomic_write_json(st, {"ts": now, "ends": len(ends)})
        if rearm:
            # window elapsed: the push goes out (the storm stays
            # visible) and the window reopens from this end
            return True
        log_event(
            f"session_end push suppressed (churn: {len(ends)} ends in "
            f"10m, project {ctx.session['project_key']}, "
            f"session {int(ctx.session['nnn']):03d})")
        return False
    except Exception:
        log_error("SessionEnd: push storm guard error (failing open)")
        return True


def _cleanup_progress_pane() -> None:
    """Phase 12 (mode 1): kill the 1-line progress pane of THIS tmux window
    and drop its record entry. The per-window data file is KEPT: the pane
    is dead anyway, and dropping it leaves a recreated pane on the "◆ …"
    placeholder until the next status line render — none comes while the
    model is down (a dropped file is how the pane sat on "…" through a
    30-minute server freeze). The next session's pane sees at most ~2 s of
    stale text before its first render overwrites it.

    The window id is resolved from ``$TMUX_PANE`` (the hook still runs in
    this CC's pane, so it is alive). No env, no record, no entry for this
    window -> no-op. Fully wrapped: the hook must never fail because of
    this.
    """
    try:
        _cleanup_progress_pane_impl()
    except Exception:
        pass


def _cleanup_progress_pane_impl() -> None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    from .. import progress_pane  # local: keeps hook imports light

    record = progress_pane._read_pane_record()
    if not record:
        return
    bin_ = os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"
    win = progress_pane._tmux_call(subprocess.run, bin_,
                                   ["display-message", "-p", "-t", pane,
                                    "#{window_id}"])
    # server-scoped key: window ids are per-server, so the bare window id
    # would match (and kill) the OTHER server's entry for the same "@N"
    key = progress_pane._pane_record_key(win) if win else None
    if not key or key not in record:
        return
    entry = record[key]
    pid = entry.get("pane_id") if isinstance(entry, dict) else None
    if pid:
        progress_pane._tmux_call(subprocess.run, bin_,
                                 ["kill-pane", "-t", pid])
    del record[key]
    progress_pane._write_pane_record(record)
    # the per-window data file is intentionally KEPT — see the docstring.
