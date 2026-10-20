"""state.py: PRD §8 state machine — validated transitions under the project lock.

Every transition goes through one function that locks, validates, writes
atomically, and appends a ledger event. Same-state calls are idempotent no-ops.
"""

import threading

import pytest

from cc_continuity import ledger, lock, paths, state

KEY = "proj-1a2b3c4d"


def _create(nnn=1, session_id="sid-1"):
    return state.create_session_state(KEY, "proj-001", nnn, session_id)


def _events(nnn=1):
    return ledger.read(paths.session_dir(KEY, nnn))


def test_create_writes_bootstrap_state_with_full_schema(cc_home):
    record = _create()
    on_disk = state.load_state(KEY, 1)
    assert on_disk == record
    assert on_disk["state"] == "BOOTSTRAPPING"
    assert on_disk["schema_version"] == 1
    for field in (
        "name", "nnn", "session_id", "project_key", "state", "rotation_id",
        "predecessor", "successor", "turn_active", "last_stop_ts",
        "handoff_attempts", "warned", "ratio_at_pending", "created",
        "updated", "degraded", "off",
    ):
        assert field in on_disk, field


def test_full_happy_path_chain(cc_home):
    _create()
    chain = [
        "ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
        "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING", "ROTATED",
    ]
    for target in chain:
        record, changed = state.transition(KEY, 1, target)
        assert changed is True
        assert record["state"] == target
    # ROTATED is terminal for this session.
    with pytest.raises(state.InvalidTransitionError):
        state.transition(KEY, 1, "ACTIVE")


def test_invalid_transition_rejected_and_state_unchanged(cc_home):
    _create()
    with pytest.raises(state.InvalidTransitionError):
        state.transition(KEY, 1, "ROTATING")  # BOOTSTRAPPING -> ROTATING: no
    assert state.load_state(KEY, 1)["state"] == "BOOTSTRAPPING"
    assert _events() == []  # nothing was written


def test_same_state_is_idempotent_noop(cc_home):
    _create()
    before = len(_events())
    record, changed = state.transition(KEY, 1, "BOOTSTRAPPING")
    assert changed is False
    assert record["state"] == "BOOTSTRAPPING"
    assert len(_events()) == before  # no event appended


def test_manual_rotate_from_active(cc_home):
    _create()
    state.transition(KEY, 1, "ACTIVE")
    record, _ = state.transition(KEY, 1, "ROTATION_PENDING")  # /continuity-rotate
    assert record["state"] == "ROTATION_PENDING"


def test_rotator_abort_returns_to_verified(cc_home):
    _create()
    for target in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                   "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"):
        state.transition(KEY, 1, target)
    # Idle check failed: back to CHECKPOINT_VERIFIED, next Stop retries.
    record, changed = state.transition(KEY, 1, "CHECKPOINT_VERIFIED")
    assert changed is True and record["state"] == "CHECKPOINT_VERIFIED"


def test_handoff_failed_degraded_path(cc_home):
    _create()
    for target in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED"):
        state.transition(KEY, 1, target)
    state.transition(KEY, 1, "HANDOFF_FAILED")
    state.transition(KEY, 1, "CHECKPOINT_WRITING")
    record, _ = state.transition(KEY, 1, "CHECKPOINT_VERIFIED", degraded=True)
    assert record["degraded"] is True


def test_manual_clear_path(cc_home):
    _create()
    for target in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                   "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED"):
        state.transition(KEY, 1, target)
    state.transition(KEY, 1, "AWAITING_MANUAL_CLEAR")  # no tmux
    record, changed = state.transition(KEY, 1, "ROTATED")
    assert changed is True and record["state"] == "ROTATED"


def test_transition_stores_extra_fields_and_appends_event(cc_home):
    _create()
    state.transition(KEY, 1, "ACTIVE")
    record, _ = state.transition(KEY, 1, "ROTATION_PENDING", ratio_at_pending=0.623)
    assert record["ratio_at_pending"] == 0.623
    events = [e for e in _events() if e.get("type") == "state"]
    assert len(events) == 2
    assert events[0]["from"] == "BOOTSTRAPPING"
    assert events[0]["to"] == "ACTIVE"
    assert events[1] == {
        "type": "state", "from": "ACTIVE", "to": "ROTATION_PENDING",
        "fields": {"ratio_at_pending": 0.623}, "ts": events[1]["ts"],
    }
    assert isinstance(events[1]["ts"], float)


def test_missing_session_raises(cc_home):
    with pytest.raises(state.StateNotFoundError):
        state.transition(KEY, 99, "ACTIVE")
    with pytest.raises(state.StateNotFoundError):
        state.load_state(KEY, 99)


def test_two_sessions_are_independent(cc_home):
    _create(nnn=1)
    state.create_session_state(KEY, "proj-002", 2, "sid-2")
    state.transition(KEY, 1, "ACTIVE")
    assert state.load_state(KEY, 1)["state"] == "ACTIVE"
    assert state.load_state(KEY, 2)["state"] == "BOOTSTRAPPING"


# ---------------------------------------------------------- update_fields


def test_update_fields_sets_flags_without_state_change(cc_home):
    _create()
    record = state.update_fields(KEY, 1, turn_active=True)
    assert record["turn_active"] is True
    on_disk = state.load_state(KEY, 1)
    assert on_disk["turn_active"] is True
    assert on_disk["state"] == "BOOTSTRAPPING"  # state untouched


def test_update_fields_multiple_fields_and_no_ledger_event(cc_home):
    _create()
    before = len(_events())
    state.update_fields(KEY, 1, turn_active=False, last_stop_ts=1234.5, off=True)
    on_disk = state.load_state(KEY, 1)
    assert on_disk["turn_active"] is False
    assert on_disk["last_stop_ts"] == 1234.5
    assert on_disk["off"] is True
    assert len(_events()) == before  # flags are not ledger events


def test_update_fields_missing_session_raises(cc_home):
    with pytest.raises(state.StateNotFoundError):
        state.update_fields(KEY, 99, turn_active=True)


def test_update_fields_concurrent_writes_stay_consistent(cc_home):
    _create()

    def worker(tid):
        for i in range(15):
            state.update_fields(KEY, 1, turn_active=(tid + i) % 2 == 0)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    on_disk = state.load_state(KEY, 1)  # parses → no torn writes
    assert isinstance(on_disk["turn_active"], bool)


# ---------------------------------------------------- _transition_unlocked


def test_transition_unlocked_under_lock(cc_home):
    state.create_session_state(KEY, "proj-001", 1, "sid-1")
    with lock.held(KEY):
        record, changed = state._transition_unlocked(KEY, 1, "ACTIVE")
    assert record["state"] == "ACTIVE" and changed is True
    with lock.held(KEY):
        with pytest.raises(state.InvalidTransitionError):
            state._transition_unlocked(KEY, 1, "ROTATING")  # ACTIVE -> ROTATING: no
    assert state.load_state(KEY, 1)["state"] == "ACTIVE"


# ------------------------------------------------- Phase 5: initial_state + increment


def test_create_session_state_custom_initial_state(cc_home):
    record = state.create_session_state(KEY, "proj-001", 1, "sid-x",
                                        initial_state="ACTIVE")
    assert record["state"] == "ACTIVE"
    assert state.load_state(KEY, 1)["state"] == "ACTIVE"


def test_create_session_state_default_stays_bootstrap(cc_home):
    record = state.create_session_state(KEY, "proj-001", 1, "sid-x")
    assert record["state"] == "BOOTSTRAPPING"


def test_create_session_state_rejects_unknown_initial(cc_home):
    with pytest.raises(ValueError):
        state.create_session_state(KEY, "proj-001", 1, "sid-x",
                                   initial_state="NOPE")


def test_increment_field_locked_and_consistent(cc_home):
    state.create_session_state(KEY, "proj-001", 1, "sid-1")
    assert state.increment(KEY, 1, "tool_calls") == 1
    assert state.increment(KEY, 1, "tool_calls") == 2
    rec = state.load_state(KEY, 1)
    assert rec["tool_calls"] == 2
    assert rec["state"] == "BOOTSTRAPPING"  # no state change
    assert all(e["type"] != "state"
               for e in ledger.read(paths.session_dir(KEY, 1)))  # no ledger event


def test_increment_rejects_non_int_fields(cc_home):
    state.create_session_state(KEY, "proj-001", 1, "sid-1")
    with pytest.raises(ValueError):
        state.increment(KEY, 1, "name")
    with pytest.raises(ValueError):
        state.increment(KEY, 1, "no_such_field")


def test_increment_missing_session_raises(cc_home):
    with pytest.raises(state.StateNotFoundError):
        state.increment(KEY, 1, "tool_calls")


def test_increment_concurrent_writes_stay_consistent(cc_home):
    state.create_session_state(KEY, "proj-001", 1, "sid-1")
    results = []
    def worker():
        for _ in range(10):
            results.append(state.increment(KEY, 1, "tool_calls"))
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == list(range(1, 81))
    assert state.load_state(KEY, 1)["tool_calls"] == 80


def test_cv_to_rotated_allowed_for_claim(cc_home):
    # P6-D2: a manual /clear right after checkpoint verification (before any
    # rotation attempt) must be able to close the session directly.
    state.create_session_state(KEY, "proj-001", 1, "sid-x")
    for s in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
              "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED"):
        state.transition(KEY, 1, s)
    state.transition(KEY, 1, "ROTATED")
    assert state.load_state(KEY, 1)["state"] == "ROTATED"


def test_update_fields_unlocked_under_held_lock(cc_home):
    state.create_session_state(KEY, "proj-001", 1, "sid-x")
    with lock.held(KEY):
        state.update_fields_unlocked(KEY, 1, successor="proj-002")
    assert state.load_state(KEY, 1)["successor"] == "proj-002"


def test_update_fields_unlocked_missing_session_raises(cc_home):
    with pytest.raises(state.StateNotFoundError):
        state.update_fields_unlocked(KEY, 99, turn_active=True)


def test_close_dead_closes_active_without_successor(cc_home):
    _create()
    state.transition(KEY, 1, "ACTIVE")
    record = state.close_dead(KEY, 1, "process verified dead 2026-10-20")
    assert record["state"] == "ROTATED"
    on_disk = state.load_state(KEY, 1)
    assert on_disk["state"] == "ROTATED"
    assert on_disk["successor"] is None
    events = [e for e in _events() if e.get("type") == "state"]
    assert events[-1]["from"] == "ACTIVE"
    assert events[-1]["to"] == "ROTATED"
    assert events[-1]["fields"]["manual_close"] == \
        "process verified dead 2026-10-20"


def test_close_dead_from_bootstrapping(cc_home):
    _create()  # initial state is BOOTSTRAPPING
    record = state.close_dead(KEY, 1, "abandoned startup")
    assert record["state"] == "ROTATED"
    events = [e for e in _events() if e.get("type") == "state"]
    assert events[-1]["from"] == "BOOTSTRAPPING"


def test_close_dead_idempotent_on_rotated(cc_home):
    _create()
    for t in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
              "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED",
              "AWAITING_MANUAL_CLEAR", "ROTATED"):
        state.transition(KEY, 1, t)
    before = _events()
    record = state.close_dead(KEY, 1, "already terminal")
    assert record["state"] == "ROTATED"
    assert _events() == before  # nothing written
