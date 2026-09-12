"""Shared hook entry skeleton (PRD §9 contract).

Every hook: read stdin JSON; print nothing except the documented JSON; exit 0
(the Phase 3–5 ledger hooks never block); finish within 300 ms in the normal
path. On **any** internal exception: log to ``logs/errors.log``, print
nothing, exit 0 — the system must never break a Claude Code session. No-op
when disabled (global config, project opt-out, ``CC_CONTINUITY=0``, session
``off`` flag).

``run_event`` is the single entry used by ``cc-continuity hook <event>``; the
event→handler registry lives in ``cli.py`` (dependency injection keeps this
module import-cycle-free and testable).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config, paths, statusline

Handler = Callable[["HookContext"], dict | None]


@dataclass
class HookContext:
    """Everything a handler may need, resolved once per hook invocation."""

    event: str
    payload: dict
    cfg: dict
    session: dict | None  # state record for payload.session_id, or None

    @property
    def session_dir(self) -> Path | None:
        if not self.session:
            return None
        return paths.session_dir(self.session["project_key"],
                                 int(self.session["nnn"]))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_error(context: str) -> None:
    """Append ``context`` + the current traceback to ``logs/errors.log``.

    Never raises — a logging failure must not break the hook.
    """
    try:
        path = paths.logs_dir() / "errors.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {context}\n")
            f.write(traceback.format_exc().rstrip() + "\n\n")
    except OSError:
        pass


def log_event(message: str) -> None:
    """Append one line to ``logs/events.log`` (PRD §14). Never raises."""
    try:
        path = paths.logs_dir() / "events.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {message}\n")
    except OSError:
        pass


def session_start_output(additional_context: str) -> dict:
    """The documented SessionStart JSON (PRD F9: hookSpecificOutput)."""
    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": additional_context,
    }}


def _parse_payload(event: str, text: str) -> dict | None:
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        log_error(f"{event}: malformed stdin JSON: {e}")
        return None
    if not isinstance(obj, dict):
        log_error(f"{event}: stdin JSON is not an object")
        return None
    return obj


def _is_disabled(cfg: dict, session: dict | None) -> bool:
    if not config.is_enabled(cfg):
        return True
    return bool(session) and bool(session.get("off"))


def _export_session_env(session_id: object) -> None:
    """Append ``export CC_CONTINUITY_SESSION`` to ``CLAUDE_ENV_FILE`` (S11).

    Only SessionStart receives that variable from Claude Code; the export is
    what lets ``status`` and later Bash calls identify the current session.
    """
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or not isinstance(session_id, str) or not session_id:
        return
    try:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f'export CC_CONTINUITY_SESSION="{session_id}"\n')
    except OSError:
        pass


def run_event(event: str, text: str | bytes, handlers: dict[str, Handler]) -> int:
    """The PRD §9 universal hook entry. Never raises; always returns 0.

    Handlers return a dict (printed as the documented JSON) or ``None``
    (print nothing). Unknown events are silent no-ops.
    """
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        payload = _parse_payload(event, text)
        if payload is None:
            return 0
        cwd = payload.get("cwd")
        cfg = config.load_config(cwd=cwd if isinstance(cwd, str) and cwd else None)
        session = statusline.find_session_by_id(payload.get("session_id"))
        if _is_disabled(cfg, session):
            return 0
        _export_session_env(payload.get("session_id"))
        handler = handlers.get(event)
        if handler is None:
            return 0
        result = handler(HookContext(event, payload, cfg, session))
        if isinstance(result, dict):
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except Exception:
        log_error(f"{event}: unhandled exception in hook entry")
        return 0
