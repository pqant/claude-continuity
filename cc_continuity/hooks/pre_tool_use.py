"""PreToolUse hook (PRD §9) — the hard ceiling (Phase 5).

Ratio gate: fresh ratio >= hard_ratio with a pre-handoff state ->
``advance`` to HANDOFF_REQUESTED first, then the deny decision. State gate
(no metric needed): HANDOFF_REQUESTED/HANDOFF_FAILED deny with C.3;
CHECKPOINT_WRITING/VERIFIED/ROTATING (handoff accepted) deny with C.9, so
the model cannot keep working and make the handoff stale. AWAITING_MANUAL_
CLEAR / ROTATED allow (checkpoint final, user in control). The allowlist
(Read/Glob/Grep, the handoff draft, Task* tools, git status/diff/log/show/
rev-parse, cc-continuity) always passes. Silent allow otherwise.
Subagent payloads (agent_id) are skipped entirely (spec D5).
"""

from __future__ import annotations

from .. import state, thresholds
from .common import HookContext

PRE_HANDOFF = frozenset({"BOOTSTRAPPING", "ACTIVE", "WARNED",
                         "ROTATION_PENDING"})
DENY_C3 = frozenset({"HANDOFF_REQUESTED", "HANDOFF_FAILED"})
DENY_C9 = frozenset({"CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"})


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None or ctx.payload.get("agent_id"):
        return None
    key, nnn = ctx.session["project_key"], int(ctx.session["nnn"])
    cfg = ctx.cfg
    draft_path = str(ctx.session_dir / "handoff.draft.md")
    rec = state.load_state(key, nnn)
    s = rec["state"]
    ratio = None
    if s in PRE_HANDOFF:
        ratio = thresholds.read_ratio(ctx)
        if ratio is not None and ratio >= cfg["hard_ratio"]:
            rec, _ = thresholds.advance(key, nnn, "HANDOFF_REQUESTED",
                                        ratio=ratio)
            s = rec["state"]
    if s in DENY_C3:
        reason = thresholds.c3_text(ratio, cfg["hard_ratio"], draft_path)
    elif s in DENY_C9:
        reason = thresholds.c9_text()
    else:
        return None
    tool = ctx.payload.get("tool_name")
    tool_input = _dict(ctx.payload.get("tool_input"))
    if thresholds.is_allowed_tool(tool, tool_input, draft_path):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
