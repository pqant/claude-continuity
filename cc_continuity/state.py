"""Per-session state machine (PRD §8) — one ``transition()`` for everything.

State lives in ``sessions/<NNN>/state.json``. Every transition takes the
project lock, validates the move, writes atomically, and appends an event to
the session ledger. A transition to the current state is an idempotent no-op
(repeated hooks must be harmless). ``ROTATED`` is terminal for a session.

Failure states (PRD §8): ``HANDOFF_FAILED`` → deterministic fallback →
``CHECKPOINT_VERIFIED(degraded)``. ``ROTATION_FAILED`` is an *event*, not a
state: the session stays in ``CHECKPOINT_VERIFIED``. A rotator abort (idle
check) moves ``ROTATING`` back to ``CHECKPOINT_VERIFIED``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import atomic, ledger, lock, paths


class StateNotFoundError(FileNotFoundError):
    """No state.json for this session (yet)."""


class InvalidTransitionError(RuntimeError):
    """The state machine does not permit this move."""


#: All states the machine can be in.
STATES = frozenset({
    "ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
    "HANDOFF_FAILED", "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED",
    "ROTATING", "AWAITING_MANUAL_CLEAR", "ROTATED", "BOOTSTRAPPING",
})

#: Permitted moves (PRD §8 diagram).
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"WARNED", "ROTATION_PENDING"}),
    "WARNED": frozenset({"ROTATION_PENDING"}),
    "ROTATION_PENDING": frozenset({"HANDOFF_REQUESTED"}),
    "HANDOFF_REQUESTED": frozenset({"CHECKPOINT_WRITING", "HANDOFF_FAILED"}),
    "HANDOFF_FAILED": frozenset({"CHECKPOINT_WRITING"}),
    "CHECKPOINT_WRITING": frozenset({"CHECKPOINT_VERIFIED"}),
    "CHECKPOINT_VERIFIED": frozenset({"ROTATING", "AWAITING_MANUAL_CLEAR"}),
    "ROTATING": frozenset({"ROTATED", "CHECKPOINT_VERIFIED"}),
    "AWAITING_MANUAL_CLEAR": frozenset({"ROTATED"}),
    "BOOTSTRAPPING": frozenset({"ACTIVE"}),
    "ROTATED": frozenset(),  # terminal for this session
}


def _state_path(project_key: str, nnn: int) -> Path:
    return paths.session_dir(project_key, nnn) / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_session_state(project_key: str, name: str, nnn: int,
                         session_id: str,
                         predecessor: str | None = None) -> dict:
    """Create a new session record in ``BOOTSTRAPPING`` (PRD §14 schema v1)."""
    record = {
        "schema_version": 1,
        "name": name,
        "nnn": nnn,
        "session_id": session_id,
        "project_key": project_key,
        "state": "BOOTSTRAPPING",
        "rotation_id": None,
        "predecessor": predecessor,
        "successor": None,
        "turn_active": False,
        "last_stop_ts": 0,
        "handoff_attempts": 0,
        "warned": False,
        "ratio_at_pending": None,
        "created": _now(),
        "updated": _now(),
        "degraded": False,
        "off": False,
    }
    sdir = paths.session_dir(project_key, nnn)
    sdir.mkdir(parents=True, exist_ok=True)
    atomic.atomic_write_json(_state_path(project_key, nnn), record)
    return record


def load_state(project_key: str, nnn: int) -> dict:
    path = _state_path(project_key, nnn)
    if not path.is_file():
        raise StateNotFoundError(f"no state for {project_key} session {nnn:03d}")
    return json.loads(path.read_text(encoding="utf-8"))


def transition(project_key: str, nnn: int, new_state: str,
               **fields: object) -> tuple[dict, bool]:
    """Validate and apply one transition. Returns ``(record, changed)``.

    ``changed`` is False when ``new_state`` equals the current state
    (idempotent no-op: nothing is written, no event is appended).
    Extra ``fields`` (e.g. ``ratio_at_pending``, ``degraded``) are stored on
    the record when the transition is applied.
    """
    if new_state not in STATES:
        raise InvalidTransitionError(f"unknown state {new_state!r}")
    with lock.held(project_key):
        record = load_state(project_key, nnn)  # raises StateNotFoundError
        current = record["state"]
        if new_state == current:
            return record, False
        if new_state not in VALID_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"{current} -> {new_state} not allowed (session {nnn:03d})"
            )
        record["state"] = new_state
        record["updated"] = _now()
        for key, value in fields.items():
            record[key] = value
        atomic.atomic_write_json(_state_path(project_key, nnn), record)
        ledger.append(
            paths.session_dir(project_key, nnn),
            {"type": "state", "from": current, "to": new_state, "fields": fields},
        )
        return record, True
