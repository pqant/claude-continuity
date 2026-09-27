"""SessionEnd hook (PRD §9).

Ledger entry with reason; if reason == ``clear`` while state is ``ROTATING``,
log it to ``events.log`` (successor confirmation happens at SessionStart,
Phase 6). If reason == ``resume``, the startup's process was abandoned
when it resumed a different conversation (relaunch with --continue): mark
the session ``off`` so it does not linger as a live zombie in scans
(regression 2026-09-27 047).
"""

from __future__ import annotations

import os
import subprocess

from .. import ledger, notify, state
from .common import HookContext, log_error, log_event


def handle(ctx: HookContext) -> dict | None:
    reason = ctx.payload.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "other"
    if ctx.session is None:
        return None
    ledger.append(ctx.session_dir, {"type": "session_end", "reason": reason})
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
