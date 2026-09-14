"""hooks/stop.py: turn-end enforcement (PRD §9, spec §6.4).

Attempt ladder: RP -> HR + block C.2; HR without a valid draft -> block
(C.6 when the draft exists and is invalid, else C.2); 3 accumulated
failed attempts -> degraded checkpoint (C.7), NO block, systemMessage.
A valid draft (even one Stop itself discovers, e.g. Bash-written) ->
finalize -> AMCL. CW -> finalize. CV -> AMCL WITHOUT re-finalizing.
Finalize failure is swallowed (errors.log, state kept).
"""

import json

from cc_continuity import checkpoint, ledger, paths, state
from hookutil import (make_repo, payload, register_session, run_hook,
                      stdout_json, walk_to)

AMCL = "Checkpoint ready. Type /clear to continue automatically."


def _setup(cc_home, tmp_path, s=None):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    if s:
        walk_to(key, nnn, s)
    return repo, key, nnn


def _valid_draft():
    bodies = [
        ("Objective", "Implement the feature end to end in the sandbox repo."),
        ("Current Task and Exact Stopping Point",
         "Just finished the edit; the test run has not happened yet."),
        ("Completed This Session", "- Added the module and its imports."),
        ("Next Atomic Action", "Run `pytest -q` and fix any failure that appears."),
        ("Remaining Plan", "1. Tests. 2. Docs."),
        ("Decisions and Rationale", "Kept stdlib only, per the constraint."),
        ("Rejected Approaches — Do Not Repeat", "No third-party dependencies."),
        ("Open Problems, Risks, Questions for the User", "None known at the moment."),
        ("Runtime Environment Notes", "No running servers or ports."),
    ]
    return "\n".join(f"## {h}\n{b}\n" for h, b in bodies)


def test_active_stop_only_updates_fields(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE")
    run_hook("Stop", payload("Stop", repo, stop_hook_active=True))
    assert stdout_json(capsys) is None
    rec = state.load_state(key, nnn)
    assert rec["turn_active"] is False
    assert rec["last_stop_ts"] > 0
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "stop"
    assert entry["stop_hook_active"] is True  # recorded, but NOT used (D6)


def test_pending_stop_walks_to_handoff_and_blocks_c2(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING")
    run_hook("Stop", payload("Stop", repo))
    out = stdout_json(capsys)
    assert out["decision"] == "block"
    assert "Rotation pending" in out["reason"]
    rec = state.load_state(key, nnn)
    assert rec["state"] == "HANDOFF_REQUESTED"
    assert rec["handoff_attempts"] == 1


def test_handoff_requested_no_draft_blocks_c2_and_counts(cc_home, capsys,
                                                         tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    run_hook("Stop", payload("Stop", repo))
    out = stdout_json(capsys)
    assert out["decision"] == "block"
    assert "Rotation pending" in out["reason"]
    assert state.load_state(key, nnn)["handoff_attempts"] == 1
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys)["decision"] == "block"
    assert state.load_state(key, nnn)["handoff_attempts"] == 2


def test_third_failed_stop_degrades_checkpoint(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    state.update_fields(key, nnn, handoff_attempts=2)
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys) == {"systemMessage": AMCL}  # NO block
    rec = state.load_state(key, nnn)
    assert rec["state"] == "AWAITING_MANUAL_CLEAR"
    snap = json.loads((paths.session_dir(key, nnn) / "checkpoint" /
                       "snapshot.json").read_text())
    assert snap["degraded"] is True
    walk = [(e["from"], e["to"]) for e in ledger.read(paths.session_dir(key, nnn))
            if e["type"] == "state"]
    assert walk[-4:] == [("HANDOFF_REQUESTED", "HANDOFF_FAILED"),
                         ("HANDOFF_FAILED", "CHECKPOINT_WRITING"),
                         ("CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED"),
                         ("CHECKPOINT_VERIFIED", "AWAITING_MANUAL_CLEAR")]


def test_handoff_requested_invalid_draft_blocks_c6(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        "## Objective\nshort\n", encoding="utf-8")
    run_hook("Stop", payload("Stop", repo))
    out = stdout_json(capsys)
    assert out["decision"] == "block"
    assert "handoff draft invalid" in out["reason"]
    assert "missing heading:" in out["reason"]
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_handoff_requested_valid_draft_finalizes(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _valid_draft(), encoding="utf-8")
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    rec = state.load_state(key, nnn)
    assert rec["state"] == "AWAITING_MANUAL_CLEAR"
    assert rec["degraded"] is False
    cp = paths.session_dir(key, nnn) / "checkpoint"
    assert (cp / "handoff.md").is_file()
    assert "## Next Atomic Action" in (cp / "handoff.md").read_text()


def test_checkpoint_writing_finalizes(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_WRITING")
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(key, nnn)["state"] == "AWAITING_MANUAL_CLEAR"
    assert (paths.session_dir(key, nnn) / "checkpoint" /
            "handoff.md").is_file()


def test_checkpoint_verified_moves_to_amcl_without_refinalize(
        cc_home, capsys, tmp_path):
    """Regression guard: CV must NOT re-run finalize (it would reject the
    state); only the AMCL transition + systemMessage happen."""
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_VERIFIED")
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(key, nnn)["state"] == "AWAITING_MANUAL_CLEAR"
    assert not (paths.session_dir(key, nnn) / "checkpoint").exists()


def test_finalize_failure_is_swallowed(cc_home, capsys, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise checkpoint.CheckpointError("simulated kill")
    monkeypatch.setattr(checkpoint, "finalize", boom)
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_WRITING")
    run_hook("Stop", payload("Stop", repo))
    assert stdout_json(capsys) is None  # the stop is allowed
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_WRITING"
    err = cc_home / "logs" / "errors.log"
    assert err.is_file() and "checkpoint finalization failed" in err.read_text()
