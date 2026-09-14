"""Threshold logic (PRD §7/§8/§9, Phase 5 spec §4) — the single source of
truth for ratio reading, state walking, the PreToolUse allowlist, and the
Appendix C injected texts.

Ratio source is the status-line metric only (PRD §7/F5): a missing or
> 10-minute-old metric is "unknown" and NEVER drives a transition; the
affected paths log METRIC_STALE.
"""

from __future__ import annotations

import math
import re

from . import checkpoint, statusline
from .hooks.common import HookContext, log_event

#: PRD §9 PreToolUse allowlist: Bash commands that stay allowed at the
#: ceiling (verbatim regex from the PRD).
_BASH_ALLOWED_RE = re.compile(r"^(git (status|diff|log|show|rev-parse)|cc-continuity )")
_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep"})
_EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def pct_of(ratio: float) -> int:
    """Integer percentage, statusline convention (ceil of a rounded value)."""
    return math.ceil(round(ratio * 100, 6))


def read_ratio(ctx: HookContext) -> float | None:
    """Fresh ratio for decisions, or None (missing/stale metric).

    Logs one METRIC_STALE line to events.log on the unknown path (PRD §7).
    """
    metric = statusline.read_metric(ctx.session["session_id"])
    if statusline.is_stale(metric):
        log_event(f"METRIC_STALE session={ctx.session['session_id']} "
                  f"state={ctx.session.get('state')}")
        return None
    ratio = metric.get("ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        return ratio
    return None


def display_ratio(ctx: HookContext, cfg: dict) -> float:
    """A ratio for C-text display: fresh, else the stale metric, else hard."""
    metric = statusline.read_metric(ctx.session["session_id"])
    if metric:
        ratio = metric.get("ratio")
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            return float(ratio)
    return float(cfg.get("hard_ratio", 0.72))


def is_allowed_tool(tool, tool_input: dict, draft_path: str) -> bool:
    """PRD §9 ceiling allowlist (silent pass)."""
    if tool in _READONLY_TOOLS:
        return True
    if isinstance(tool, str) and tool.startswith("Task"):
        return True
    if tool in _EDIT_TOOLS:
        return tool_input.get("file_path") == draft_path
    if tool == "Bash":
        command = tool_input.get("command")
        return (isinstance(command, str)
                and _BASH_ALLOWED_RE.match(command) is not None)
    return False


# ------------------------------------------------------- Appendix C texts


def c1_text(ratio: float, window: int) -> str:
    return (f"CONTINUITY: context at {pct_of(ratio)}% of {window}. A session "
            "rotation will be needed soon. Keep working, but do not start a "
            "large new subtask without first reaching a clean stopping point.")


def c2_text(ratio: float, draft_path: str) -> str:
    headings = ", ".join(checkpoint.TEMPLATE_HEADINGS)
    return (f"CONTINUITY: context at {pct_of(ratio)}%. Rotation pending. "
            f"Finish only the step you are in (at most: complete the current "
            f"edit and run its quick check). Then write the handoff to "
            f"{draft_path} using exactly these headings: {headings}. Only "
            "write what you know that git/logs cannot show. Then end your "
            "turn.")


def c3_text(ratio: float | None, hard_ratio: float, draft_path: str) -> str:
    shown = ratio if ratio is not None else hard_ratio
    return (f"CONTINUITY: context at {pct_of(shown)}% (hard limit). Tool "
            f"blocked. Write the handoff now to {draft_path} with the "
            "required headings, then end your turn. Allowed now: Write/Edit "
            "of that file, Read/Glob/Grep, git status/diff/log/show.")


def c6_text(problems: list, draft_path: str) -> str:
    return ("CONTINUITY: handoff draft invalid: " + "; ".join(problems)
            + f". Fix {draft_path} and end your turn.")


def c9_text() -> str:
    # Phase-5 tuning (spec §4): no "will rotate automatically" — rotation
    # is a manual /clear until Phase 6.
    return ("CONTINUITY: handoff accepted. Do not start any more work in "
            "this session. End your turn now.")
