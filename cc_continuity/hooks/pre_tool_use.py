"""PreToolUse hook (PRD §9) — the hard ceiling (Phase 5).

Ratio gate: fresh ratio >= hard_ratio with a pre-handoff state ->
``advance`` to HANDOFF_REQUESTED, let the in-flight call through and inject
the C.2 handoff instruction as neutral additionalContext (issue #25: CC
renders denials as red Errors; routine rotation is not an error). State
gate (no metric needed): HANDOFF_REQUESTED/HANDOFF_FAILED deny with C.3;
CHECKPOINT_WRITING/VERIFIED/ROTATING (handoff accepted) deny with C.9, so
the model cannot keep working and make the handoff stale. AWAITING_MANUAL_
CLEAR / ROTATED allow (checkpoint final, user in control). The allowlist
(Read/Glob/Grep, the handoff draft, Task* tools, git status/diff/log/show/
rev-parse, cc-continuity) always passes. Silent allow otherwise.
Subagent payloads (agent_id) are skipped entirely (spec D5).
"""

from __future__ import annotations

from .. import ledger, state, thresholds
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
            thresholds.advance(key, nnn, "HANDOFF_REQUESTED", ratio=ratio,
                               cfg=cfg)
            # Guidance-first (issue #25): this call was queued before the
            # model could learn rotation started, and CC renders any denial
            # as a red "Error:" line - routine rotation is not an error.
            # Let the in-flight call through and inject the C.2 instruction
            # as neutral context instead; denial is reserved for calls the
            # model makes AFTER seeing the guidance.
            ledger.gate(ctx.session_dir, action="guide", state_from=s,
                        state_to="HANDOFF_REQUESTED", ratio=ratio,
                        variant="c2")
            return {"systemMessage": thresholds.c2_note(ratio),
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": thresholds.c2_text(
                            ratio, draft_path,
                            blocked=ledger.blocked_reason(ctx.session_dir)),
                    }}
    if s in DENY_C3:
        blocked = ledger.blocked_reason(ctx.session_dir)
        guidance = thresholds.c3_text(ratio, cfg["hard_ratio"], draft_path,
                                      blocked=blocked)
        reason = thresholds.c3_brief(ratio, cfg["hard_ratio"], draft_path,
                                     blocked=blocked)
        variant = "c3"
    elif s in DENY_C9:
        guidance = thresholds.c9_text()
        reason = thresholds.c9_brief()
        variant = "c9"
    else:
        return None
    tool = ctx.payload.get("tool_name")
    tool_input = _dict(ctx.payload.get("tool_input"))
    if thresholds.is_allowed_tool(tool, tool_input, draft_path):
        return None
    # Only REAL denials are gate decisions (allowlisted calls pass
    # silently); the record exists for post-hoc rotation debugging (#39).
    ledger.gate(ctx.session_dir, action="deny", state_from=s, state_to=s,
                ratio=ratio, variant=variant, tool=str(tool))
    # The full gate guidance rides in systemMessage so the UI shows an
    # informational note, not a red error; the model still acts on the
    # brief deny reason.
    return {"systemMessage": guidance, "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
