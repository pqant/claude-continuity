"""SessionEnd hook (PRD §9).

Ledger entry with reason; if reason == ``clear`` while state is ``ROTATING``,
log it to ``events.log`` (successor confirmation happens at SessionStart,
Phase 6).
"""

from __future__ import annotations

import os
import subprocess

from .. import ledger, notify
from .common import HookContext, log_event


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
    _cleanup_progress_pane()
    return None


def _cleanup_progress_pane() -> None:
    """Phase 12 (mode 1): kill the 1-line progress pane of THIS tmux window
    and drop its record entry + per-window data file (no stale % for the
    next session that reuses the window).

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
    if not win or win not in record:
        return
    entry = record[win]
    pid = entry.get("pane_id") if isinstance(entry, dict) else None
    if pid:
        progress_pane._tmux_call(subprocess.run, bin_,
                                 ["kill-pane", "-t", pid])
    del record[win]
    progress_pane._write_pane_record(record)
    progress_pane.drop_progress_data(win)
