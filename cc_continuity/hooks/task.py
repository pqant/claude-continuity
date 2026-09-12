"""TaskCreated / TaskCompleted hooks (PRD §9).

Ledger entry per event; ``TaskCompleted`` is the boundary signal consumed by
threshold logic (Phase 5).
"""

from __future__ import annotations

from .. import ledger
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
    return None
