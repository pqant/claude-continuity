"""SessionStart hook (PRD §9) — Phase 3: ``startup`` registration;
Phase 5: initial state (spec D3) + ``compact`` re-injection (spec D1);
Phase 6: ``clear`` claim + C.4 bootstrap (P6-D2/D3).

Register the project + session (name from the ``CC_CONTINUITY_NAME`` env
var set by the Phase 7 wrapper, else the project counter). Plain
startups register in ACTIVE; with a pending checkpoint (Phase 4
``pending_consumption``) the session starts in BOOTSTRAPPING and gets
the C.5 notice. ``compact`` injects a short post-compaction state block
(spec §6.7). ``resume``/``fork`` are no-op re-attaches (spec D1) that re-record the
``tmux_session`` field (a resume may land in a NEW tmux session —
regression 2026-09-27 046).
``clear`` claims the latest claimable session (ROTATING / AMCL /
CHECKPOINT_VERIFIED, successor unset) — successor record + C.4
injection + ``runtime/<sid>.started`` — or registers exactly like a
plain startup when nothing is claimable (P6-D3). The
``CC_CONTINUITY_SESSION`` env export (S11) happens in the common
skeleton for every event that carries ``CLAUDE_ENV_FILE``.
"""

from __future__ import annotations

import json
import math
import os
import subprocess

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
        # CAPTURE_FAILED while the session looks fine. Re-attach only:
        # no registration, no state change, no output.
        if ctx.session is not None:
            _record_tmux_session(ctx.session["project_key"],
                                 int(ctx.session["nnn"]))
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
            _register(ctx, key, session_id)
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
            _record_tmux_session(key, res["new_nnn"])
            return session_start_output(res["bootstrap_text"])
        if ctx.session is None:
            _register(ctx, key, session_id)
        return _pending_notice(ctx, key)
    except Exception:
        log_error(f"SessionStart(clear): claim failed for {session_id}")
        return None


def _register(ctx: HookContext, key: str, session_id: str) -> None:
    record = project.ensure_project(ctx.payload["cwd"])
    sessions = record.get("sessions") or []
    predecessor = sessions[-1]["name"] if sessions else None
    name, nnn = _decide_name(key, record["slug"])
    initial = "BOOTSTRAPPING" if _pending_checkpoint(key) else "ACTIVE"
    state.create_session_state(key, name, nnn, session_id,
                               predecessor=predecessor, initial_state=initial)
    project.record_session(key, name, session_id, nnn)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "session_start", "source": "startup",
                   "name": name, "nnn": nnn})
    notify.safe_send(ctx.cfg, "session_started",
                     slug=notify.project_slug(key), nnn=nnn, name=name,
                     host=notify.host())
    _record_tmux_session(key, nnn)


def _decide_name(key: str, slug: str) -> tuple[str, int]:
    """Wrapper-supplied name wins when it ends in ``-<digits>`` (PRD §13)."""
    env_name = os.environ.get("CC_CONTINUITY_NAME", "").strip()
    _base, seq = naming.split_name(env_name)
    if seq is not None:
        return env_name, project.next_counter(key, requested=seq)
    nnn = project.next_counter(key)
    return naming.logical_name(slug, nnn), nnn


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def _record_tmux_session(key: str, nnn: int) -> None:
    """P10-D9.1: record the pane's tmux session name (the watchdog's
    primary nudge target).

    Only when ``$TMUX`` is set; one fast subprocess (2 s timeout — the
    hook keeps its ~100 ms budget in the common case); every failure
    path is swallowed — this must never break the hook (P10-D12).
    """
    if not os.environ.get("TMUX"):
        return
    try:
        proc = subprocess.run(
            [_tmux_bin(), "display-message", "-p", "#{session_name}"],
            capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    name = proc.stdout.decode(errors="replace").strip()
    if not name:
        return
    try:
        state.update_fields(key, nnn, tmux_session=name)
    except Exception:
        pass


def _pending_notice(ctx: HookContext, key: str) -> dict | None:
    cp = _pending_checkpoint(key)
    if cp is None:
        return None
    return session_start_output(_c5_text(cp, key))


def _pending_checkpoint(key: str) -> dict | None:
    try:
        rec = json.loads(
            (paths.project_dir(key) / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cp = rec.get("latest_checkpoint")
    if isinstance(cp, dict) and cp.get("pending_consumption"):
        return cp
    return None


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
