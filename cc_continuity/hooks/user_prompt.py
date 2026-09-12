"""UserPromptSubmit hook (PRD §9).

Append ``{type:user_prompt, text:redact(truncate(prompt, 2000))}`` to the
ledger and set ``turn_active=true`` (locked field update, no ledger event).

The ``CONTINUITY RESUME`` prefix handling (checkpoint consumption,
BOOTSTRAPPING→ACTIVE) arrives with the Phase 6 bootstrap.
"""

from __future__ import annotations

from .. import ledger, redact, state
from .common import HookContext

MAX_PROMPT_CHARS = 2000  # PRD §9, verbatim


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None:
        return None
    prompt = ctx.payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = "" if prompt is None else str(prompt)
    text = redact.Redactor(ctx.cfg.get("redact_patterns")).redact(
        prompt[:MAX_PROMPT_CHARS])[0]
    ledger.append(ctx.session_dir, {"type": "user_prompt", "text": text})
    state.update_fields(ctx.session["project_key"], int(ctx.session["nnn"]),
                        turn_active=True)
    return None
