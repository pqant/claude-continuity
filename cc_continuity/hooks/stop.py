"""Stop hook (PRD §9) — turn-end enforcement (Phase 5, spec §6.4).

State-based (never ratio-based): ROTATION_PENDING -> advance to
HANDOFF_REQUESTED + attempt/block; HANDOFF_REQUESTED -> validate the
draft file DIRECTLY (a Bash-written draft was never seen by
PostToolUse) -> finalize or attempt/block; 3 accumulated failed
attempts -> degrade (HANDOFF_FAILED -> deterministic checkpoint, C.7);
CHECKPOINT_WRITING -> finalize; CHECKPOINT_VERIFIED -> AMCL without
re-running finalize; everything else: ``turn_active=False`` +
``last_stop_ts`` only. After a verified checkpoint the session ALWAYS
ends in AWAITING_MANUAL_CLEAR with the AMCL systemMessage (spec D2; the
tmux/ROTATING branch is Phase 6). Subagent payloads (agent_id) are
skipped entirely (spec D5). ``stop_hook_active`` is recorded in the
ledger but NOT used — the attempts counter is authoritative (spec D6).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import checkpoint, ledger, paths, state, statusline, thresholds
from .common import HookContext, log_error

AMCL_MESSAGE = "Checkpoint ready. Type /clear to continue automatically."


def _project_root(key: str) -> Path:
    pfile = paths.project_dir(key) / "project.json"
    return Path(json.loads(pfile.read_text(encoding="utf-8"))["root"])


def _finalize_and_amcl(ctx: HookContext, key: str, nnn: int) -> dict | None:
    try:
        metric = statusline.read_metric(ctx.session["session_id"])
        checkpoint.finalize(key, nnn, cfg=ctx.cfg, root=_project_root(key),
                            metric=metric)
    except checkpoint.CheckpointError as e:
        # Logged; the stop is allowed; the next Stop retries (finalize
        # re-cleans checkpoint.tmp-* on every run).
        log_error(f"Stop: checkpoint finalization failed: {e}")
        return None
    state.transition(key, nnn, "AWAITING_MANUAL_CLEAR")
    return {"systemMessage": AMCL_MESSAGE}


def _degrade(ctx: HookContext, key: str, nnn: int) -> dict | None:
    state.transition(key, nnn, "HANDOFF_FAILED")
    # finalize walks HF -> CW -> CV(degraded) — the draft is missing or
    # invalid here, so the deterministic-only C.7 checkpoint is built.
    return _finalize_and_amcl(ctx, key, nnn)


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None or ctx.payload.get("agent_id"):
        return None
    key, nnn = ctx.session["project_key"], int(ctx.session["nnn"])
    sdir = ctx.session_dir
    cfg = ctx.cfg
    draft_path = str(sdir / "handoff.draft.md")
    ledger.append(sdir, {"type": "stop",
                         "stop_hook_active": bool(ctx.payload.get("stop_hook_active"))})
    rec = state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    s = rec["state"]
    tool_calls = rec.get("tool_calls", 0)
    if s == "ROTATION_PENDING":
        rec, _ = thresholds.advance(key, nnn, "HANDOFF_REQUESTED")
        attempts = state.update_fields(key, nnn,
                                       handoff_attempts=rec.get("handoff_attempts", 0) + 1,
                                       ctx_emitted_at=tool_calls)["handoff_attempts"]
        if attempts >= 3:
            return _degrade(ctx, key, nnn)
        return {"decision": "block",
                "reason": thresholds.c2_text(thresholds.display_ratio(ctx, cfg),
                                             draft_path)}
    if s == "HANDOFF_REQUESTED":
        draft = sdir / "handoff.draft.md"
        report = None
        if draft.is_file():
            report = checkpoint.validate_draft(draft.read_text(encoding="utf-8"),
                                               cfg)
        if report is not None and report.ok:
            state.transition(key, nnn, "CHECKPOINT_WRITING")
            return _finalize_and_amcl(ctx, key, nnn)
        attempts = state.update_fields(key, nnn,
                                       handoff_attempts=rec.get("handoff_attempts", 0) + 1,
                                       ctx_emitted_at=tool_calls)["handoff_attempts"]
        if attempts >= 3:
            return _degrade(ctx, key, nnn)
        text = (thresholds.c6_text(report.problems, draft_path) if report is not None
                else thresholds.c2_text(thresholds.display_ratio(ctx, cfg), draft_path))
        return {"decision": "block", "reason": text}
    if s == "CHECKPOINT_WRITING":
        return _finalize_and_amcl(ctx, key, nnn)
    if s == "CHECKPOINT_VERIFIED":
        state.transition(key, nnn, "AWAITING_MANUAL_CLEAR")
        return {"systemMessage": AMCL_MESSAGE}
    return None
