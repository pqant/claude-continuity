"""SessionStart hook (PRD §9) — Phase 3: ``startup`` registration;
Phase 5: initial state (spec D3) + ``compact`` re-injection (spec D1);
Phase 6: ``clear`` claim + C.4 bootstrap (P6-D2/D3).

Register the project + session (name from the ``CC_CONTINUITY_NAME`` env
var set by the Phase 7 wrapper, else the project counter). Plain
startups register in ACTIVE; with a pending checkpoint (Phase 4
``pending_consumption``) the session starts in BOOTSTRAPPING and gets
the C.5 notice. ``compact`` injects a short post-compaction state block
(spec §6.7). ``resume``/``fork`` re-attach (spec D1) when the conversation has a record —
re-recording the ``tmux_session`` field (a resume may land in a NEW tmux
session — regression 2026-09-27 046) — and register a NEW record when it does
not (Phase 27: CC's native auto-resume leaves the resumed conversation
unmonitored).
``clear`` claims the latest claimable session (ROTATING / AMCL /
CHECKPOINT_VERIFIED, successor unset) — successor record + C.4
injection + ``runtime/<sid>.started`` — or registers exactly like a
plain startup when nothing is claimable (P6-D3). The
``CC_CONTINUITY_SESSION`` env export (S11) happens in the common
skeleton for every event that carries ``CLAUDE_ENV_FILE``.
"""

from __future__ import annotations

import math

from .. import (bootstrap, checkpoint, ledger, lock, naming, notify, paths,
                project, state, statusline)
from .common import HookContext, log_error, notify_claimed, session_start_output


def handle(ctx: HookContext) -> dict | None:
    source = ctx.payload.get("source")
    if source == "compact":
        return _compact_notice(ctx)
    if source == "clear":
        return _clear(ctx)
    if source in ("resume", "fork"):
        # regression 2026-09-27 046: resuming a conversation in a NEW tmux
        # session (relaunch with --continue) must re-record the pane, or
        # the field stays stale (dead pane) and every nudge/digest
        # CAPTURE_FAILED while the session looks fine.
        if ctx.session is not None:
            # Re-attach only: no registration, no state change, no output.
            bootstrap.record_tmux_session(ctx.session["project_key"],
                                          int(ctx.session["nnn"]))
            return None
        # Phase 27: a resumed conversation with NO matching record runs
        # unmonitored — CC's native auto-resume (lastSessionId) abandons
        # the startup session ~5 s after launch and the old conversation
        # has no record of its own (2026-10-02: connector 067-071,
        # a7c36f64 unmonitored for 3 weeks). Register it.
        cwd = ctx.payload.get("cwd")
        session_id = ctx.payload.get("session_id")
        if not isinstance(cwd, str) or not cwd:
            return None
        if not isinstance(session_id, str) or not session_id:
            return None
        try:
            root = project.resolve_root(cwd)
            key = project.project_key_for_root(root)
            _register(ctx, session_id, source=source)
            return _pending_notice(ctx, key)
        except Exception:
            log_error(f"SessionStart({source}): registration failed for "
                      f"{session_id}")
            return None
    if source != "startup":
        return None
    cwd = ctx.payload.get("cwd")
    session_id = ctx.payload.get("session_id")
    if not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        root = project.resolve_root(cwd)
        key = project.project_key_for_root(root)
        if ctx.session is None:
            _register(ctx, session_id)
        return _pending_notice(ctx, key)
    except Exception:
        log_error(f"SessionStart(startup): registration failed for {session_id}")
        return None


def _clear(ctx: HookContext) -> dict | None:
    """PRD §9 SessionStart/clear: claim a rotation, else plain register.

    Claim (P6-D2/P6-D3): the latest session in ROTATING / AMCL /
    CHECKPOINT_VERIFIED with ``successor`` unset is claimed under the
    project lock; the successor is created (BOOTSTRAPPING), the old session
    closed (ROTATED), the checkpoint consumed, and the C.4 block (protocol +
    validation result + handoff or path) is injected. ``runtime/<sid>.started``
    is written for the rotator (PRD §11.6). Without a claimable session the
    new conversation registers exactly like a plain startup.
    """
    cwd = ctx.payload.get("cwd")
    session_id = ctx.payload.get("session_id")
    if not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        root = project.resolve_root(cwd)
        key = project.project_key_for_root(root)
        with lock.held(key):
            res = bootstrap.try_claim(key, new_session_id=session_id,
                                      cfg=ctx.cfg)
        if res is not None:
            bootstrap.write_started(session_id)
            notify_claimed(ctx.cfg, key, res)
            bootstrap.record_tmux_session(key, res["new_nnn"])
            return session_start_output(res["bootstrap_text"])
        if ctx.session is None:
            _register(ctx, session_id)
        return _pending_notice(ctx, key)
    except Exception:
        log_error(f"SessionStart(clear): claim failed for {session_id}")
        return None


def _register(ctx: HookContext, session_id: str,
              source: str = "startup") -> None:
    bootstrap.register_session(ctx.payload["cwd"], session_id,
                               cfg=ctx.cfg, source=source,
                               allow_env=(source == "startup"))


def _pending_notice(ctx: HookContext, key: str) -> dict | None:
    cp = bootstrap.pending_checkpoint(key)
    if cp is None:
        return None
    return session_start_output(_c5_text(cp, key))


def _compact_notice(ctx: HookContext) -> dict | None:
    """Spec §6.7 — post-compaction re-injection (< 4 KB target)."""
    rec = ctx.session
    if rec is None:
        return None
    lines = [f"CONTINUITY (post-compaction state): session {rec['name']} "
             f"({int(rec['nnn']):03d}), state {rec['state']}."]
    metric = statusline.read_metric(rec["session_id"])
    if metric and not statusline.is_stale(metric):
        r = metric.get("ratio")
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            lines.append(f"context at {math.ceil(round(r * 100, 6))}%.")
    ls = checkpoint.ledger_summary(ctx.session_dir)
    if ls["files_touched"]:
        lines.append("Files touched (last 5): "
                     + ", ".join(f["path"] for f in ls["files_touched"][:5]))
    if ls["commands"]:
        lines.append("Commands (last 3): "
                     + " | ".join(c["command"][:80] for c in ls["commands"][-3:]))
    if ls["failed_commands"]:
        lines.append("Failed commands (last 2): " + "; ".join(
            f"{f['tool']}: {f.get('error_first_line') or '?'}"
            for f in ls["failed_commands"][-2:]))
    if ls["tasks"]:
        lines.append("Tasks (last 5): " + "; ".join(
            f"{t['id']} {t['subject'] or ''} [{t['status']}]"
            for t in ls["tasks"][-5:]))
    if ls["user_prompts"]:
        lines.append("Recent prompts (last 2): " + " | ".join(
            p[:100] for p in ls["user_prompts"][-2:]))
    if rec["state"] in ("ROTATION_PENDING", "HANDOFF_REQUESTED", "HANDOFF_FAILED"):
        lines.append("Rotation will happen at the next boundary.")
    return session_start_output("\n".join(lines)[:4000])


def _c5_text(cp: dict, key: str) -> str:
    """Appendix C.5: short notice naming the pending checkpoint."""
    cp_id = cp.get("cp_id", "?")
    when = cp.get("created", "?")
    handoff_path = cp.get("handoff_path")
    if not handoff_path and isinstance(cp.get("nnn"), int):
        handoff_path = str(paths.session_dir(key, cp["nnn"]) / "checkpoint"
                           / "handoff.md")
    return (f"CONTINUITY: an unconsumed checkpoint {cp_id} from {when} exists "
            f"for this project ({handoff_path or '?'}). If the user asks to "
            f"continue, follow the resume protocol.")
