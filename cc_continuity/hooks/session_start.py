"""SessionStart hook (PRD §9) — Phase 3: ``startup`` registration only.

Register the project + session (name from the ``CC_CONTINUITY_NAME`` env var
set by the Phase 7 wrapper, else the project counter), create the state record
in BOOTSTRAPPING, append the ledger entry, and inject the short C.5 notice
when a checkpoint is ``pending_consumption``.

Other sources (``clear``/``compact``/``resume``/``fork``) do not register
here: ``clear`` bootstrap is Phase 6, ``compact``/``resume`` injections are
Phase 5. The ``CC_CONTINUITY_SESSION`` env export (S11) happens in the common
skeleton for every event that carries ``CLAUDE_ENV_FILE``.
"""

from __future__ import annotations

import json
import os

from .. import ledger, naming, paths, project, state
from .common import HookContext, log_error, session_start_output


def handle(ctx: HookContext) -> dict | None:
    if ctx.payload.get("source") != "startup":
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


def _register(ctx: HookContext, key: str, session_id: str) -> None:
    record = project.ensure_project(ctx.payload["cwd"])
    sessions = record.get("sessions") or []
    predecessor = sessions[-1]["name"] if sessions else None
    name, nnn = _decide_name(key, record["slug"])
    state.create_session_state(key, name, nnn, session_id, predecessor=predecessor)
    project.record_session(key, name, session_id, nnn)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "session_start", "source": "startup",
                   "name": name, "nnn": nnn})


def _decide_name(key: str, slug: str) -> tuple[str, int]:
    """Wrapper-supplied name wins when it ends in ``-<digits>`` (PRD §13)."""
    env_name = os.environ.get("CC_CONTINUITY_NAME", "").strip()
    _base, seq = naming.split_name(env_name)
    if seq is not None:
        return env_name, project.next_counter(key, requested=seq)
    nnn = project.next_counter(key)
    return naming.logical_name(slug, nnn), nnn


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
