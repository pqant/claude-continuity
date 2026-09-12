"""Hook event handlers (PRD §9).

Phase 3 implements the ledger hooks: SessionStart (startup registration),
UserPromptSubmit, PostToolUse/PostToolUseFailure (ledger part),
TaskCreated/TaskCompleted, SessionEnd.

Phase 5–6 add the enforcement hooks: PreToolUse (ceiling/deny allowlist),
Stop (handoff forcing, rotator spawn), PreCompact (emergency snapshot), and
the SessionStart ``clear``/``compact``/``resume`` paths.

Every handler: read stdin JSON, print only the documented JSON, exit 0
unless it intends to block; ≤ 300 ms normal path (Stop may take ≤ 5 s while
finalizing a checkpoint); on any exception → log to ``logs/errors.log``,
print nothing, exit 0 — the system must never break a Claude Code session;
no-op when disabled (config, project opt-out, ``CC_CONTINUITY=0``, session
``off`` flag).
"""
