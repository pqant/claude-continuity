"""UserPromptSubmit hook (PRD §9).

Append ``{type:user_prompt, text:redact(truncate(prompt, 2000))}`` to the
ledger and set ``turn_active=true`` (locked field update).

Phase 6 (bootstrap): a prompt starting with ``CONTINUITY RESUME`` consumes
the checkpoint it names; ANY prompt moves a BOOTSTRAPPING session to
ACTIVE (P6-D4 — the PRD mandates the RESUME case, the superset keeps
assisted/crash flows from sticking in BOOTSTRAPPING). A RESUME prompt from
an UNREGISTERED session id performs the claim first (P6-D5 / PRD §18:
SessionStart did not fire after /clear, the continuation prompt is
self-sufficient).
"""

from __future__ import annotations

import json
import re

from .. import (atomic, bootstrap, config, ledger, lock, paths, project,
                redact, state)
from .common import HookContext, log_error, notify_claimed

MAX_PROMPT_CHARS = 2000  # PRD §9, verbatim
_RESUME_PREFIX = "CONTINUITY RESUME"
_CP_RE = re.compile(r"cp-(\d+)")


def _prompt(ctx: HookContext) -> str:
    prompt = ctx.payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = "" if prompt is None else str(prompt)
    return prompt


def handle(ctx: HookContext) -> dict | None:
    prompt = _prompt(ctx)
    is_resume = prompt.startswith(_RESUME_PREFIX)
    rec = ctx.session
    if rec is None:
        if not is_resume:
            return None
        rec = _fallback_claim(ctx, prompt)
        if rec is None:
            return None
    key, nnn = rec["project_key"], int(rec["nnn"])
    sdir = paths.session_dir(key, nnn)
    text = redact.Redactor(config.redact_patterns(ctx.cfg)).redact(
        prompt[:MAX_PROMPT_CHARS])[0]
    entry = {"type": "user_prompt", "text": text}
    agent_id = ctx.payload.get("agent_id")
    if agent_id:
        # Subagent prompt: record it (tagged, so the rotator idle check
        # ignores it) but never touch main-session state.
        entry["agent_id"] = agent_id
        ledger.append(sdir, entry)
        return None
    ledger.append(sdir, entry)
    try:
        current = state.load_state(key, nnn)
    except state.StateNotFoundError:
        return None
    if current["state"] == "BOOTSTRAPPING":
        state.transition(key, nnn, "ACTIVE")
    if is_resume:
        _consume_resume(key, nnn, rec.get("name"), prompt)
    state.update_fields(key, nnn, turn_active=True)
    return None


def _fallback_claim(ctx: HookContext, prompt: str) -> dict | None:
    """P6-D5 — claim for an unregistered session id on a RESUME prompt."""
    cwd = ctx.payload.get("cwd")
    session_id = ctx.payload.get("session_id")
    if not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        key = project.project_key_for_root(project.resolve_root(cwd))
        with lock.held(key):
            res = bootstrap.try_claim(key, new_session_id=session_id,
                                      cfg=ctx.cfg)
        if res is None:
            return None
        bootstrap.write_started(session_id)
        notify_claimed(ctx.cfg, key, res)
        return state.load_state(key, res["new_nnn"])
    except Exception:
        log_error(f"UserPromptSubmit: RESUME claim failed for {session_id}")
        return None


def _consume_resume(key: str, nnn: int, name: str | None, prompt: str) -> None:
    """Clear ``pending_consumption`` on the checkpoint named in the prompt."""
    m = _CP_RE.search(prompt)
    if not m:
        return
    target = int(m.group(1))
    pfile = paths.project_dir(key) / "project.json"
    try:
        with lock.held(key):
            pref = json.loads(pfile.read_text(encoding="utf-8"))
            cp = pref.get("latest_checkpoint")
            if isinstance(cp, dict) and cp.get("nnn") == target \
                    and cp.get("pending_consumption"):
                cp = dict(cp)
                cp["pending_consumption"] = False
                if name:
                    cp["consumed_by"] = name
                pref["latest_checkpoint"] = cp
                atomic.atomic_write_json(pfile, pref)
    except (OSError, json.JSONDecodeError) as e:
        log_error(f"UserPromptSubmit: checkpoint consumption failed: {e}")
