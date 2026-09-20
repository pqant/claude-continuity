"""SessionEnd hook (PRD §9).

Ledger entry with reason; if reason == ``clear`` while state is ``ROTATING``,
log it to ``events.log`` (successor confirmation happens at SessionStart,
Phase 6).
"""

from __future__ import annotations

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
    return None
