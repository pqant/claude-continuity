"""Stop hook (PRD §9) — turn-end enforcement (Phase 5, spec §6.4).

State-based (never ratio-based): ROTATION_PENDING -> advance to
HANDOFF_REQUESTED + attempt/block; HANDOFF_REQUESTED -> validate the
draft file DIRECTLY (a Bash-written draft was never seen by
PostToolUse) -> finalize or attempt/block; 3 accumulated failed
attempts -> degrade (HANDOFF_FAILED -> deterministic checkpoint, C.7);
CHECKPOINT_WRITING -> finalize; CHECKPOINT_VERIFIED -> AMCL without
re-running finalize; everything else: ``turn_active=False`` +
``last_stop_ts`` only. After a verified checkpoint the session rotates
itself when ``$TMUX_PANE`` + ``full_auto`` are set (detached rotator +
ROTATING, P6-D8); otherwise it ends in AWAITING_MANUAL_CLEAR with the
AMCL systemMessage (spec D2). Subagent payloads (agent_id) are
skipped entirely (spec D5). ``stop_hook_active`` is recorded in the
ledger but NOT used — the attempts counter is authoritative (spec D6).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .. import (checkpoint, ledger, notify, paths, rotator, state, statusline,
                thresholds)
from .common import HookContext, log_error

AMCL_MESSAGE = "Checkpoint ready. Type /clear to continue automatically."


def _project_root(key: str) -> Path:
    pfile = paths.project_dir(key) / "project.json"
    return Path(json.loads(pfile.read_text(encoding="utf-8"))["root"])


def _finalize(ctx: HookContext, key: str, nnn: int) -> bool:
    """Finalize the checkpoint (state -> CHECKPOINT_VERIFIED inside).
    Failure is logged and the stop is allowed; the next Stop retries
    (finalize re-cleans checkpoint.tmp-* on every run)."""
    try:
        metric = statusline.read_metric(ctx.session["session_id"])
        checkpoint.finalize(key, nnn, cfg=ctx.cfg, root=_project_root(key),
                            metric=metric)
    except checkpoint.CheckpointError as e:
        log_error(f"Stop: checkpoint finalization failed: {e}")
        notify.safe_send(ctx.cfg, "error", slug=notify.project_slug(key),
                         nnn=nnn, dedup_key=f"finalize:{nnn:03d}",
                         what="checkpoint finalization",
                         detail=str(e)[:60])
        return False
    return True


def _rotate_or_amcl(ctx: HookContext, key: str, nnn: int,
                    current: str) -> dict | None:
    """PRD §9 Stop, three branches:

    1. pane + full_auto ($TMUX_PANE) -> detached rotator + ROTATING
       (P6-D8) — the pane rotates itself; no systemMessage.
    2. paneless + full_auto (no $TMUX_PANE: the session was launched
       outside tmux) -> paneless self-heal: spawn the successor in a
       DETACHED tmux pane (rotation must work in every situation);
       completed -> systemMessage naming the successor + attach
       command; deferred/exited -> silent (state handles the retry);
       failed -> assisted fallback below.
    3. assisted -> AWAITING_MANUAL_CLEAR + the AMCL systemMessage."""
    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane and ctx.cfg.get("full_auto", True):
        rotation_id = str(uuid.uuid4())
        try:
            rotator.spawn(ctx.session["session_id"], pane, rotation_id)
        except rotator.RotatorSpawnError as e:
            log_error(f"Stop: rotator spawn failed, assisted fallback: {e}")
            notify.safe_send(ctx.cfg, "error", slug=notify.project_slug(key),
                             nnn=nnn, dedup_key=f"spawn:{rotation_id}",
                             what="rotator spawn", detail=str(e)[:60])
        else:
            if current == "ROTATING":
                # Orphan recovery re-spawn: already ROTATING, and a
                # self transition is a no-op — refresh the id directly.
                state.update_fields(key, nnn, rotation_id=rotation_id)
            else:
                try:
                    state.transition(key, nnn, "ROTATING",
                                     rotation_id=rotation_id)
                except state.InvalidTransitionError as e:
                    # Claim raced us (old session already ROTATED): the
                    # spawned rotator self-exits on its state re-read
                    # (PRD §11.2).
                    log_error(f"Stop: ROTATING transition raced: {e}")
            return None  # no systemMessage — the pane rotates itself
    if not pane and ctx.cfg.get("full_auto", True):
        # Paneless self-heal: no pane to drive, so the successor is a
        # fresh claude in a DETACHED tmux pane (rotation must work in
        # every situation). The successor's SessionStart(startup) claims
        # the ROTATING project; the old session is terminal ROTATED when
        # it completes.
        rotation_id = str(uuid.uuid4())
        if current == "ROTATING":
            state.update_fields(key, nnn, rotation_id=rotation_id)
        else:
            try:
                state.transition(key, nnn, "ROTATING",
                                 rotation_id=rotation_id)
            except state.InvalidTransitionError as e:
                log_error(f"Stop: paneless ROTATING transition raced: {e}")
                return None
        try:
            outcome, info = rotator.spawn_successor_detached(
                key, nnn, ctx.session["session_id"], ctx.cfg, rotation_id)
        except rotator.RotatorSpawnError as e:
            log_error(f"Stop: paneless spawn failed, assisted fallback: {e}")
            notify.safe_send(ctx.cfg, "error",
                             slug=notify.project_slug(key), nnn=nnn,
                             dedup_key=f"paneless_spawn:{rotation_id}",
                             what="paneless successor spawn", detail=str(e)[:60])
            outcome, info = "failed", None
        if outcome == "completed":
            return {"systemMessage": (
                f"Rotated to {info['name']} — it runs in tmux session "
                f"{info['tmux_session']} (attach with: tmux attach -t "
                f"{info['tmux_session']}). This session is superseded; "
                "continue there.")}
        if outcome in ("deferred", "exited"):
            return None
        if current == "CHECKPOINT_VERIFIED":
            # The state is ROTATING now (moved above): roll it back to CV
            # first (the legal defer transition — ROTATING -> AMCL does
            # not exist), then take the assisted path.
            state.transition(key, nnn, "CHECKPOINT_VERIFIED")
            state.transition(key, nnn, "AWAITING_MANUAL_CLEAR")
            return {"systemMessage": AMCL_MESSAGE}
        return None
    if current == "CHECKPOINT_VERIFIED":
        state.transition(key, nnn, "AWAITING_MANUAL_CLEAR")
        return {"systemMessage": AMCL_MESSAGE}
    # ROTATING without tmux/full_auto (env changed mid-session): stay, and
    # the next Stop retries. No message — the state line shows ROTATING.
    return None


def _degrade(ctx: HookContext, key: str, nnn: int) -> dict | None:
    state.transition(key, nnn, "HANDOFF_FAILED")
    # finalize walks HF -> CW -> CV(degraded) — the draft is missing or
    # invalid here, so the deterministic-only C.7 checkpoint is built.
    if not _finalize(ctx, key, nnn):
        return None
    return _rotate_or_amcl(ctx, key, nnn, "CHECKPOINT_VERIFIED")


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
        rec, _ = thresholds.advance(key, nnn, "HANDOFF_REQUESTED", cfg=cfg)
        attempts = state.update_fields(key, nnn,
                                       handoff_attempts=rec.get("handoff_attempts", 0) + 1,
                                       ctx_emitted_at=tool_calls)["handoff_attempts"]
        if attempts >= 3:
            return _degrade(ctx, key, nnn)
        return {"decision": "block",
                "reason": thresholds.c2_text(thresholds.display_ratio(ctx, cfg),
                                             draft_path,
                                             blocked=ledger.blocked_reason(sdir))}
    if s == "HANDOFF_REQUESTED":
        draft = sdir / "handoff.draft.md"
        report = None
        if draft.is_file():
            report = checkpoint.validate_draft(draft.read_text(encoding="utf-8"),
                                               cfg)
        if report is not None and report.ok:
            state.transition(key, nnn, "CHECKPOINT_WRITING")
            if not _finalize(ctx, key, nnn):
                return None
            return _rotate_or_amcl(ctx, key, nnn, "CHECKPOINT_VERIFIED")
        attempts = state.update_fields(key, nnn,
                                       handoff_attempts=rec.get("handoff_attempts", 0) + 1,
                                       ctx_emitted_at=tool_calls)["handoff_attempts"]
        if attempts >= 3:
            return _degrade(ctx, key, nnn)
        text = (thresholds.c6_text(report.problems, draft_path) if report is not None
                else thresholds.c2_text(thresholds.display_ratio(ctx, cfg), draft_path,
                                        blocked=ledger.blocked_reason(sdir)))
        return {"decision": "block", "reason": text}
    if s == "CHECKPOINT_WRITING":
        if not _finalize(ctx, key, nnn):
            return None
        return _rotate_or_amcl(ctx, key, nnn, "CHECKPOINT_VERIFIED")
    if s == "CHECKPOINT_VERIFIED":
        return _rotate_or_amcl(ctx, key, nnn, "CHECKPOINT_VERIFIED")
    if s == "ROTATING":
        # Orphan recovery (PRD §18 "retry on next Stop"): spawn again — the
        # rotation lock guarantees at most one live rotator.
        return _rotate_or_amcl(ctx, key, nnn, "ROTATING")
    return None
