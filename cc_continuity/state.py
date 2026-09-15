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
    "CHECKPOINT_VERIFIED": frozenset({"ROTATING", "AWAITING_MANUAL_CLEAR",
                                      "ROTATED"}),
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
                         predecessor: str | None = None,
                         initial_state: str = "BOOTSTRAPPING") -> dict:
    """Create a new session record (PRD §14 schema v1).

    ``initial_state`` defaults to BOOTSTRAPPING; Phase 5 registers plain
    startups directly in ACTIVE (spec D3).
    """
    if initial_state not in STATES:
        raise ValueError(f"unknown initial state {initial_state!r}")
    record = {
        "schema_version": 1,
        "name": name,
        "nnn": nnn,
        "session_id": session_id,
        "project_key": project_key,
        "state": initial_state,
        "rotation_id": None,
        "predecessor": predecessor,
        "successor": None,
        "turn_active": False,
        "last_stop_ts": 0,
        "handoff_attempts": 0,
        "warned": False,
        "ratio_at_pending": None,
        "boundary_pending": False,  # spec §5
        "tool_calls": 0,            # spec §5
        "ctx_emitted_at": None,     # spec §5
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


def update_fields(project_key: str, nnn: int, **fields: object) -> dict:
    """Update non-state fields (``turn_active``, ``off``, …) under the lock.

    No transition validation, no ledger event — for flags the state machine
    does not model (PRD §9 hooks). Raises :class:`StateNotFoundError` like
    :func:`load_state` when the session does not exist.
    """
    if not fields:
        return load_state(project_key, nnn)
    with lock.held(project_key):
        return update_fields_unlocked(project_key, nnn, **fields)


def update_fields_unlocked(project_key: str, nnn: int, **fields: object) -> dict:
    """Locked-field write WITHOUT taking the project lock (caller holds it).

    Same contract as :func:`update_fields` minus the lock — for
    multi-record operations that already hold the project lock (checkpoint
    finalization, Phase 6 bootstrap claim). No transition validation, no
    ledger event. Raises :class:`StateNotFoundError` like :func:`load_state`.
    """
    if not fields:
        return load_state(project_key, nnn)
    record = load_state(project_key, nnn)
    record.update(fields)
    record["updated"] = _now()
    atomic.atomic_write_json(_state_path(project_key, nnn), record)
    return record


def increment(project_key: str, nnn: int, field: str) -> int:
    """Locked read-increment-write of an int counter field.

    No transition validation, no ledger event — for counters the state
    machine does not model (``tool_calls``, PRD §9 Phase 5). Returns the
    new value.
    """
    with lock.held(project_key):
        record = load_state(project_key, nnn)  # raises StateNotFoundError
        value = record.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"field {field!r} is not an int: {value!r}")
        record[field] = value + 1
        record["updated"] = _now()
        atomic.atomic_write_json(_state_path(project_key, nnn), record)
        return record[field]


def _transition_unlocked(project_key: str, nnn: int, new_state: str,
                         **fields: object) -> tuple[dict, bool]:
    """One transition — caller must hold the project lock.

    Returns ``(record, changed)``; ``changed`` is False when ``new_state``
    equals the current state (idempotent no-op: nothing written, no event).
    """
    if new_state not in STATES:
        raise InvalidTransitionError(f"unknown state {new_state!r}")
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


def transition(project_key: str, nnn: int, new_state: str,
               **fields: object) -> tuple[dict, bool]:
    """Validate and apply one transition (takes the project lock)."""
    with lock.held(project_key):
        return _transition_unlocked(project_key, nnn, new_state, **fields)
