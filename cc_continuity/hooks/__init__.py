"""Hook event handlers (PRD §9). Implemented in Phases 3–5.

Every handler: read stdin JSON, print only the documented JSON, exit 0
unless it intends to block; ≤ 300 ms normal path (Stop may take ≤ 5 s while
finalizing a checkpoint); on any exception → log to ``logs/errors.log``,
print nothing, exit 0 — the system must never break a Claude Code session;
no-op when disabled (config, project opt-out, ``CC_CONTINUITY=0``, session
``off`` flag).

Events: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PostToolUseFailure, TaskCreated, TaskCompleted, Stop, PreCompact,
SessionEnd.
"""
