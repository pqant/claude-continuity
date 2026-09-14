"""TaskCreated / TaskCompleted hooks (PRD §9).

Ledger entry per event — subagent payloads included (Phase 3 behaviour,
spec D5). ``TaskCompleted`` additionally sets ``boundary_pending=true``
when the MAIN session is in ROTATION_PENDING (spec D4: the NEXT
PostToolUse consumes the flag; this event itself stays silent).
"""

from __future__ import annotations

from .. import ledger, state
from .common import HookContext


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    entry = {
        "type": "task_created" if ctx.event == "TaskCreated" else "task_completed",
        "task_id": ctx.payload.get("task_id"),
        "subject": ctx.payload.get("task_subject"),
    }
    ledger.append(ctx.session_dir, entry)
    if (ctx.event == "TaskCompleted" and not ctx.payload.get("agent_id")
            and state.load_state(ctx.session["project_key"],
                                 int(ctx.session["nnn"]))["state"]
            == "ROTATION_PENDING"):
        state.update_fields(ctx.session["project_key"],
                            int(ctx.session["nnn"]), boundary_pending=True)
    return None
