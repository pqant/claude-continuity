"""Append-only session ledger (``sessions/<NNN>/ledger.jsonl``, PRD §14).

Hooks fire concurrently (S1/N1), so appends use a single ``os.write`` on an
``O_APPEND`` descriptor — POSIX guarantees the offset bump + write of one
call is atomic, so parallel hooks never interleave or tear lines. Each entry
gets a float ``ts`` (epoch seconds) unless the caller supplies one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

LEDGER_NAME = "ledger.jsonl"


def _path(session_dir: str | Path) -> Path:
    return Path(session_dir) / LEDGER_NAME


def append(session_dir: str | Path, entry: dict) -> dict:
    """Append one entry (single atomic line); returns it with ``ts``."""
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    path = _path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return entry


def read(session_dir: str | Path) -> list[dict]:
    """All entries in order; missing ledger → empty list."""
    path = _path(session_dir)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def gate(session_dir: str | Path, *, action: str, state_from: str,
         state_to: str, ratio: float | None = None,
         variant: str | None = None, **extra) -> dict | None:
    """Append a compact gate-decision record (issue #39): which decision
    fired, its state transition, ratio and the C-text variant used.
    Read-only for the model; consumed by logs and post-hoc debugging.
    A failed append must never change hook behaviour, so OSError is
    swallowed here and None is returned (blink-style discipline)."""
    entry: dict = {"type": "gate", "action": action,
                   "from": state_from, "to": state_to}
    if ratio is not None:
        entry["ratio"] = round(float(ratio), 4)
    if variant:
        entry["variant"] = variant
    entry.update(extra)
    try:
        return append(session_dir, entry)
    except OSError:
        return None


def blocked_reason(session_dir: str | Path) -> str | None:
    """P9-P5: reason of the latest UNCONSUMED ``needs_human`` entry, or
    None. A later main-session ``user_prompt`` entry (no ``agent_id``)
    consumes the block; subagent prompts do not. Never raises — a
    missing/corrupt ledger reads as unblocked."""
    try:
        entries = read(session_dir)
    except (OSError, ValueError):
        return None
    blocked = None
    for entry in entries:
        t = entry.get("type")
        if t == "needs_human":
            blocked = entry.get("reason")
        elif t == "user_prompt" and not entry.get("agent_id"):
            blocked = None
    return blocked


# Entries that mean "the session woke itself and kept working": a Stop
# whose reply is followed by any of these was never a wait for the user.
_RESUME_TYPES = frozenset({"tool_use", "tool_failure", "task_created",
                           "task_completed", "stop"})


def reply_pending(session_dir: str | Path, *, settle: float = 0.0,
                  now: float | None = None) -> float | None:
    """Ts of the latest UNCONSUMED ``reply_ready`` entry, or None.

    Written by the Stop hook when a turn ends waiting for the user;
    the tab blinks until read. A later main-session ``user_prompt``
    (no ``agent_id``) or a ``reply_read`` entry (the poller's focus
    dwell) consumes it; subagent prompts do not. Never raises.

    Settle (user ruling 2026-11-07, opt-in via ``settle``): a reply
    younger than the window is not reported yet, giving an
    auto-continuation a chance to arrive. Any work/event entry newer
    than the reply (a wake type in ``_RESUME_TYPES``) marks that the
    session resumed on its own (background task, cron fire, late tool
    run): the reply is then consumed silently, because a session that
    wakes itself was never waiting for the user. ``now`` is injectable
    for tests; ``settle=0`` keeps the raw read.
    """
    try:
        entries = read(session_dir)
    except (OSError, ValueError):
        return None
    pending = None
    for entry in entries:
        t = entry.get("type")
        if t == "reply_ready":
            pending = entry.get("ts")
        elif (t == "user_prompt" and not entry.get("agent_id")) or t == "reply_read":
            pending = None
    if pending is None:
        return None
    now = time.time() if now is None else now
    if settle > 0 and now - pending < settle:
        return None  # settling: an auto-continue may still land
    for entry in entries:
        if (entry.get("type") in _RESUME_TYPES
                and (entry.get("ts") or 0) > pending):
            return None  # woke itself: never a wait for the user
    return pending


def mark_reply_read(session_dir: str | Path) -> None:
    """Append the focus-dwell consumption event for ``reply_ready``."""
    append(session_dir, {"type": "reply_read"})
