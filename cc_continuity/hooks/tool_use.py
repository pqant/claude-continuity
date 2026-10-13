"""PostToolUse / PostToolUseFailure hooks (PRD §9).

``PostToolUse``: tool name; edit tools → ``file_path``; Bash → redacted
command (≤ 500 chars) + interrupted flag (S1: a successful Bash response has
no exit-code field, only ``interrupted``); task tools → task_id/subject/status.
Main-session payloads additionally get Phase 5 threshold evaluation: the
spec §6.2 decision-table walks (C.1/C.2), boundary detection
(``boundary_pending`` for the next event), and draft-write validation
(C.6/C.9) with ``additionalContext`` emission.

``PostToolUseFailure``: ``{type:tool_failure, tool, error_first_line}``,
plus ``command`` (redacted, ≤ 500) for Bash — S1/N1 established that a
failed Bash raises this event, NOT ``PostToolUse``.

Threshold evaluation, boundary detection, and ``additionalContext``
emission (C.1/C.2/C.6/C.9) are implemented in Phase 5.
"""

from __future__ import annotations

import re

from .. import checkpoint, config, ledger, redact, state, thresholds
from .common import HookContext

EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
MAX_COMMAND_CHARS = 500  # PRD §9: "redacted command (≤ 500 chars)"

_BASH_BOUNDARY_RE = re.compile(r"^git commit")
_ROTATE_FROM = frozenset({"BOOTSTRAPPING", "ACTIVE", "WARNED"})
_DRAFT_STATES = frozenset({"HANDOFF_REQUESTED", "HANDOFF_FAILED"})


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _tool_entry(ctx: HookContext) -> dict:
    tool = ctx.payload.get("tool_name")
    tool_input = _dict(ctx.payload.get("tool_input"))
    entry: dict = {"type": "tool_use", "tool": tool}
    if tool in EDIT_TOOLS:
        if tool_input.get("file_path"):
            entry["file_path"] = tool_input["file_path"]
    elif tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            entry["command"] = redact.Redactor(config.redact_patterns(ctx.cfg)
                                               ).redact(command)[0][:MAX_COMMAND_CHARS]
        if _dict(ctx.payload.get("tool_response")).get("interrupted"):
            entry["interrupted"] = True
    elif isinstance(tool, str) and tool.startswith("Task"):
        for in_key, out_key in (("taskId", "task_id"), ("task_id", "task_id"),
                                ("subject", "subject"), ("status", "status")):
            if tool_input.get(in_key) is not None:
                entry[out_key] = tool_input[in_key]
    return entry


def _is_boundary_signal(ctx: HookContext) -> bool:
    """PRD §8 boundary signals that arrive as PostToolUse: a successful
    ``git commit``, a test/build/lint command (config regex), or a
    TaskUpdate to completed. (TaskCompleted events set the flag in
    task.py; Stop walks directly.)"""
    tool = ctx.payload.get("tool_name")
    tool_input = _dict(ctx.payload.get("tool_input"))
    if tool == "TaskUpdate":
        response = _dict(ctx.payload.get("tool_response"))
        change = response.get("statusChange")
        to = change.get("to") if isinstance(change, dict) else None
        return (tool_input.get("status") == "completed"
                or to == "completed")
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            return (bool(_BASH_BOUNDARY_RE.match(command))
                    or bool(re.search(ctx.cfg.get("boundary_command_regex", ""),
                                      command)))
    return False


def _draft_report(ctx: HookContext):
    """validate_draft of the session draft, when this event wrote it."""
    if ctx.payload.get("tool_name") not in EDIT_TOOLS:
        return None
    file_path = _dict(ctx.payload.get("tool_input")).get("file_path")
    draft = ctx.session_dir / "handoff.draft.md"
    if not file_path or str(draft) != file_path or not draft.is_file():
        return None
    return checkpoint.validate_draft(draft.read_text(encoding="utf-8"),
                                     ctx.cfg)


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    key, nnn = ctx.session["project_key"], int(ctx.session["nnn"])
    sdir = ctx.session_dir
    entry = _tool_entry(ctx)
    agent_id = ctx.payload.get("agent_id")
    if agent_id:
        entry["agent_id"] = agent_id
    ledger.append(sdir, entry)
    if agent_id:
        return None  # subagent: no Phase 5 logic (spec D5)
    cfg = ctx.cfg
    draft_path = str(sdir / "handoff.draft.md")
    rec = state.load_state(key, nnn)
    tool_calls = state.increment(key, nnn, "tool_calls")
    ratio = thresholds.read_ratio(ctx)
    s = rec["state"]
    emitted: str | None = None
    if s == "ROTATION_PENDING" and rec.get("boundary_pending"):
        rec, changed = thresholds.advance(key, nnn, "HANDOFF_REQUESTED",
                                          cfg=cfg)
        if changed:
            state.update_fields(key, nnn, boundary_pending=False,
                                ctx_emitted_at=tool_calls)
            emitted = thresholds.c2_text(thresholds.display_ratio(ctx, cfg),
                                         draft_path,
                                         blocked=ledger.blocked_reason(sdir))
    elif (s in _ROTATE_FROM and ratio is not None
          and ratio >= cfg["hard_ratio"]):
        rec, changed = thresholds.advance(key, nnn, "HANDOFF_REQUESTED",
                                          ratio=ratio, cfg=cfg)
        if changed:
            state.update_fields(key, nnn, ctx_emitted_at=tool_calls)
            emitted = thresholds.c2_text(ratio, draft_path,
                                         blocked=ledger.blocked_reason(sdir))
    elif (s in {"BOOTSTRAPPING", "ACTIVE"} and ratio is not None
          and ratio >= cfg["rotate_ratio"]):
        rec, changed = thresholds.advance(key, nnn, "ROTATION_PENDING",
                                          ratio=ratio, cfg=cfg)
        if changed:
            state.update_fields(key, nnn, ctx_emitted_at=tool_calls)
            emitted = thresholds.c2_text(ratio, draft_path,
                                         blocked=ledger.blocked_reason(sdir))
    elif s == "WARNED" and ratio is not None and ratio >= cfg["rotate_ratio"]:
        rec, changed = thresholds.advance(key, nnn, "ROTATION_PENDING",
                                          ratio=ratio, cfg=cfg)
        if changed:
            state.update_fields(key, nnn, ctx_emitted_at=tool_calls)
            emitted = thresholds.c2_text(ratio, draft_path,
                                         blocked=ledger.blocked_reason(sdir))
    elif (s in {"BOOTSTRAPPING", "ACTIVE"} and ratio is not None
          and ratio >= cfg["warn_ratio"]):
        rec, changed = thresholds.advance(key, nnn, "WARNED",
                                          ratio=ratio, cfg=cfg)
        if changed:
            emitted = thresholds.c1_text(ratio, cfg["window_tokens"])
    elif s == "HANDOFF_REQUESTED":
        if tool_calls - (rec.get("ctx_emitted_at") or 0) >= 5:
            state.update_fields(key, nnn, ctx_emitted_at=tool_calls)
            emitted = thresholds.c2_text(thresholds.display_ratio(ctx, cfg),
                                         draft_path,
                                         blocked=ledger.blocked_reason(sdir))
    # boundary flag for the NEXT event — only when still pending after any
    # walk this event performed (spec D4: the boundary event itself is silent)
    if rec["state"] == "ROTATION_PENDING" and _is_boundary_signal(ctx):
        state.update_fields(key, nnn, boundary_pending=True)
    # immediate draft validation (also after a walk to HANDOFF_REQUESTED in
    # this same invocation; C.6/C.9 then supersedes the walk's C.2)
    report = _draft_report(ctx)
    if report is not None and rec["state"] in _DRAFT_STATES:
        if report.ok:
            state.transition(key, nnn, "CHECKPOINT_WRITING")
            emitted = thresholds.c9_text()
        else:
            emitted = thresholds.c6_text(report.problems, draft_path)
    if emitted is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": emitted}}


def handle_failure(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    entry: dict = {"type": "tool_failure", "tool": ctx.payload.get("tool_name")}
    if ctx.payload.get("tool_name") == "Bash":
        command = _dict(ctx.payload.get("tool_input")).get("command")
        if isinstance(command, str):
            entry["command"] = redact.Redactor(config.redact_patterns(ctx.cfg)
                                               ).redact(command)[0][:MAX_COMMAND_CHARS]
    error = ctx.payload.get("error")
    if isinstance(error, str) and error.strip():
        entry["error_first_line"] = error.strip().splitlines()[0].strip()
    agent_id = ctx.payload.get("agent_id")
    if agent_id:
        entry["agent_id"] = agent_id
    ledger.append(ctx.session_dir, entry)
    return None
