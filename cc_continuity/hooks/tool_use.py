"""PostToolUse / PostToolUseFailure hooks (PRD §9) — the ledger part.

``PostToolUse``: tool name; edit tools → ``file_path``; Bash → redacted
command (≤ 500 chars) + interrupted flag (S1: a successful Bash response has
no exit-code field, only ``interrupted``); task tools → task_id/subject/status.

``PostToolUseFailure``: ``{type:tool_failure, tool, error_first_line}`` — S1/N1
established that a failed Bash raises this event, NOT ``PostToolUse``.

Threshold evaluation and ``additionalContext`` emission (C.1/C.2) are Phase 5.
"""

from __future__ import annotations

from .. import ledger, redact
from .common import HookContext

EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
MAX_COMMAND_CHARS = 500  # PRD §9: "redacted command (≤ 500 chars)"


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
            entry["command"] = redact.Redactor(ctx.cfg.get("redact_patterns")
                                               ).redact(command)[0][:MAX_COMMAND_CHARS]
        if _dict(ctx.payload.get("tool_response")).get("interrupted"):
            entry["interrupted"] = True
    elif isinstance(tool, str) and tool.startswith("Task"):
        for in_key, out_key in (("taskId", "task_id"), ("task_id", "task_id"),
                                ("subject", "subject"), ("status", "status")):
            if tool_input.get(in_key) is not None:
                entry[out_key] = tool_input[in_key]
    return entry


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    ledger.append(ctx.session_dir, _tool_entry(ctx))
    return None


def handle_failure(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    entry: dict = {"type": "tool_failure", "tool": ctx.payload.get("tool_name")}
    error = ctx.payload.get("error")
    if isinstance(error, str) and error.strip():
        entry["error_first_line"] = error.strip().splitlines()[0].strip()
    ledger.append(ctx.session_dir, entry)
    return None
