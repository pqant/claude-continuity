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

from . import checkpoint, lock, notify, state, statusline
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


def _blocked_suffix(blocked: str | None) -> str:
    """P9-D5/P9-P5: instruction line when a needs_human entry is
    unconsumed — the handoff must tell the successor what is blocked."""
    if not blocked:
        return ""
    return (f' Add the line "BLOCKED on: {blocked}" to the '
            f'"Current Task and Exact Stopping Point" section.')


def c2_text(ratio: float, draft_path: str,
            blocked: str | None = None) -> str:
    headings = ", ".join(checkpoint.TEMPLATE_HEADINGS)
    return (f"CONTINUITY: context at {pct_of(ratio)}%. Rotation pending. "
            f"Finish only the step you are in (at most: complete the current "
            f"edit and run its quick check). Then write the handoff to "
            f"{draft_path} using exactly these headings: {headings}. Only "
            "write what you know that git/logs cannot show. Then end your "
            "turn." + _blocked_suffix(blocked))


def c3_text(ratio: float | None, hard_ratio: float, draft_path: str,
            blocked: str | None = None) -> str:
    shown = ratio if ratio is not None else hard_ratio
    return (f"CONTINUITY: context at {pct_of(shown)}% (hard limit). Tool "
            f"blocked. Write the handoff now to {draft_path} with the "
            "required headings, then end your turn. Allowed now: Write/Edit "
            "of that file, Read/Glob/Grep, git status/diff/log/show."
            + _blocked_suffix(blocked))


def c6_text(problems: list, draft_path: str) -> str:
    return ("CONTINUITY: handoff draft invalid: " + "; ".join(problems)
            + f". Fix {draft_path} and end your turn.")


def c9_text() -> str:
    # Phase-5 tuning (spec §4): no "will rotate automatically" — rotation
    # is a manual /clear until Phase 6.
    return ("CONTINUITY: handoff accepted. Do not start any more work in "
            "this session. End your turn now.")


# -------------------------------------------------------------- state walk

#: Path to ROTATION_PENDING, by current state (each step is
#: VALID_TRANSITIONS-legal; BOOTSTRAPPING goes through ACTIVE).
_PENDING_PATHS = {
    "BOOTSTRAPPING": ("ACTIVE", "ROTATION_PENDING"),
    "ACTIVE": ("ROTATION_PENDING",),
    "WARNED": ("ROTATION_PENDING",),
}

#: Path to WARNED, by current state (each step is VALID_TRANSITIONS-legal;
#: BOOTSTRAPPING goes through ACTIVE). Spec §6.2 row 5 (PostToolUse warn
#: crossing) is the only consumer.
_WARNED_PATHS = {
    "BOOTSTRAPPING": ("ACTIVE", "WARNED"),
    "ACTIVE": ("WARNED",),
}


def advance(key: str, nnn: int, target: str, *,
            ratio: float | None = None,
            cfg: dict | None = None) -> tuple[dict, bool]:
    """Walk the current state to ``target`` (PRD §8), one locked transition
    at a time. Returns ``(record, changed)``; ``changed`` is False when the
    session is already in ``target`` (idempotent no-op).

    ``target`` is ``"WARNED"``, ``"ROTATION_PENDING"`` or
    ``"HANDOFF_REQUESTED"``. For the latter the path is
    ``checkpoint.HANDOFF_WALK[current]`` (the Phase 4
    walk tables, re-used; every non-empty entry already ends at
    HANDOFF_REQUESTED, so nothing is appended). On the step entering
    ROTATION_PENDING the
    fields ``warned=True`` and — when ``ratio`` is not None —
    ``ratio_at_pending=ratio`` are written; the WARNED step always writes
    ``warned=True``. Any unreachable source (unknown
    target, backwards move, or an illegal single step) raises
    ``state.InvalidTransitionError``.

    ``cfg`` — when given, the walk fires the spec §5 row 2/3
    pushes on the steps entering WARNED / ROTATION_PENDING
    (``notify.on_state_enter``); ``None`` keeps the call silent.
    """
    if target not in ("WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED"):
        raise state.InvalidTransitionError(f"advance: unknown target {target!r}")
    with lock.held(key):
        record = state.load_state(key, nnn)
        current = record["state"]
        if current == target:
            return record, False
        if target == "WARNED":
            path = _WARNED_PATHS.get(current)
        elif target == "ROTATION_PENDING":
            path = _PENDING_PATHS.get(current)
        else:
            walk = checkpoint.HANDOFF_WALK.get(current)
            if not walk:
                raise state.InvalidTransitionError(
                    f"advance: cannot reach {target} from {current} "
                    f"(session {nnn:03d})")
            path = walk  # HANDOFF_WALK entries already end at HANDOFF_REQUESTED
        if path is None:
            raise state.InvalidTransitionError(
                f"advance: cannot reach {target} from {current} "
                f"(session {nnn:03d})")
        for step in path:
            fields = {}
            if step in ("WARNED", "ROTATION_PENDING"):
                fields = {"warned": True}
            if step == "ROTATION_PENDING" and ratio is not None:
                fields["ratio_at_pending"] = ratio
            record, _ = state._transition_unlocked(key, nnn, step, **fields)
            notify.on_state_enter(cfg, key, nnn, step, ratio=ratio)
        return record, True


def c4_text(*, new_name: str, old_name: str, status: str,
            details: list, handoff: str, handoff_path: str,
            inline_max: int, new_work: list | None = None) -> str:
    """Appendix C.4 — the SessionStart(clear) bootstrap block.

    ``details``: validation detail lines (may be empty). ``handoff`` is
    inlined when non-empty and <= ``inline_max`` chars, otherwise the path
    is shown instead (PRD §9; S5: 12K inline is safe for the local model).
    ``new_work``: user prompts that landed in the old session's live
    ledger AFTER the handoff was written (the rotation window) — they
    are not in the handoff, so they are appended as a separate section
    (empty/None → the text is exactly the old format).
    """
    lines = [f"CONTINUITY BOOTSTRAP: you are {new_name}, continuing "
             f"{old_name}. VALIDATION: {status}"]
    lines.extend(details)
    lines.append(f"Protocol: read the handoff below (or at {handoff_path}) "
                 "fully; do not redo completed work; start with Next Atomic "
                 "Action.")
    lines.append("---")
    if handoff and len(handoff) <= inline_max:
        lines.append(handoff.rstrip())
    else:
        lines.append(handoff_path)
    if new_work:
        lines.append("---")
        lines.append("NEW WORK SINCE HANDOFF (arrived after the handoff "
                     "was written; not in the handoff — add these to your "
                     "task list):")
        lines.extend(f"  > {w}" for w in new_work)
    return "\n".join(lines)


def c8_text(cp_id: str, handoff_path: str) -> str:
    """Appendix C.8 — the single-line continuation prompt the rotator types."""
    return (f"CONTINUITY RESUME {cp_id}: read {handoff_path} completely, "
            "apply the VALIDATION result, then continue from Next Atomic "
            "Action. Do not repeat completed work.")
