"""hooks/stop.py: turn-end enforcement (PRD §9, spec §6.4).

Attempt ladder: RP -> HR + block C.2; HR without a valid draft -> block
(C.6 when the draft exists and is invalid, else C.2); 3 accumulated
failed attempts -> degraded checkpoint (C.7), NO block, systemMessage.
A valid draft (even one Stop itself discovers, e.g. Bash-written) ->
finalize -> AMCL. CW -> finalize. CV -> AMCL WITHOUT re-finalizing.
Finalize failure is swallowed (errors.log, state kept).
"""

import json

import pytest

from cc_continuity import checkpoint, ledger, paths, state
from cc_continuity.hooks import stop
from hookutil import (SID, make_repo, payload, register_session, run_hook,
                      stdout_json, walk_to)

AMCL = "Checkpoint ready. Type /clear to continue automatically."


@pytest.fixture(autouse=True)
def _paneless_spawn_fails(monkeypatch):
    # The paneless self-heal spawns a REAL claude in a REAL tmux session
    # — a test must never do that. Default outcome "failed" exercises the
    # assisted AMCL fallback; the paneless-branch tests below override it.
    monkeypatch.setattr(stop.rotator, "spawn_successor_detached",
                        lambda *a, **kw: ("failed", None))


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
    # No pane here: the Stop takes the paneless self-heal branch — the
    # (fake) spawn fails, the state rolls back CV and takes the assisted
    # AMCL path (the two extra steps vs. the old direct CV->AMCL).
    assert walk[-6:] == [("HANDOFF_REQUESTED", "HANDOFF_FAILED"),
                         ("HANDOFF_FAILED", "CHECKPOINT_WRITING"),
                         ("CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED"),
                         ("CHECKPOINT_VERIFIED", "ROTATING"),
                         ("ROTATING", "CHECKPOINT_VERIFIED"),
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


# --- full-auto ROTATING branch (PRD §9 Stop, P6-D8) ---------------------------


def _verified_stop_setup(cc_home, tmp_path, s="CHECKPOINT_VERIFIED") -> dict:
    from cc_continuity import config, project

    repo = make_repo(tmp_path)
    if s == "CHECKPOINT_WRITING":
        key, nnn = register_session(repo)
        walk_to(key, nnn, "CHECKPOINT_WRITING")
        (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
            _valid_draft())
        return {"repo": repo, "key": key, "nnn": nnn}
    if s == "HANDOFF_REQUESTED":
        key, nnn = register_session(repo)
        walk_to(key, nnn, "HANDOFF_REQUESTED")
        return {"repo": repo, "key": key, "nnn": nnn}
    # CHECKPOINT_VERIFIED: real checkpoint (draft + finalize)
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _valid_draft())
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    return {"repo": repo, "key": key, "nnn": nnn}


def test_verified_stop_full_auto_spawns_rotator(cc_home, capsys, tmp_path,
                                                 monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%42")
    spawns = []
    monkeypatch.setattr(stop.rotator, "spawn",
                        lambda sid, pane, rid: (spawns.append((sid, pane, rid))
                                                 or 777))
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) is None  # no systemMessage: pane rotates itself
    rec = state.load_state(d["key"], d["nnn"])
    assert rec["state"] == "ROTATING"
    assert rec["rotation_id"]
    assert len(spawns) == 1
    assert spawns[0][0] == SID and spawns[0][1] == "%42"


def test_cw_stop_full_auto_finalizes_then_rotates(cc_home, capsys, tmp_path,
                                                   monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path, s="CHECKPOINT_WRITING")
    monkeypatch.setenv("TMUX_PANE", "%42")
    spawns = []
    monkeypatch.setattr(stop.rotator, "spawn",
                        lambda sid, pane, rid: (spawns.append((sid, pane, rid))
                                                 or 777))
    run_hook("Stop", payload("Stop", d["repo"]))
    assert state.load_state(d["key"], d["nnn"])["state"] == "ROTATING"
    assert len(spawns) == 1


def test_degraded_stop_full_auto_rotates(cc_home, capsys, tmp_path, monkeypatch):
    # 3 failed attempts -> degraded checkpoint -> still rotates (C.7 handoff)
    d = _verified_stop_setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    monkeypatch.setenv("TMUX_PANE", "%42")
    spawns = []
    monkeypatch.setattr(stop.rotator, "spawn",
                        lambda sid, pane, rid: (spawns.append((sid, pane, rid))
                                                 or 777))
    for _ in range(3):  # each Stop: attempt++ -> 3rd degrades + rotates
        run_hook("Stop", payload("Stop", d["repo"]))
    assert state.load_state(d["key"], d["nnn"])["state"] == "ROTATING"
    assert state.load_state(d["key"], d["nnn"])["degraded"] is True
    assert len(spawns) == 1


def test_full_auto_disabled_keeps_amcl(cc_home, capsys, tmp_path, monkeypatch,
                                        tmp_path_factory):
    d = _verified_stop_setup(cc_home, tmp_path)
    (d["repo"] / ".claude").mkdir(exist_ok=True)
    (d["repo"] / ".claude" / "continuity.json").write_text(
        json.dumps({"full_auto": False}))
    monkeypatch.setenv("TMUX_PANE", "%42")
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(d["key"], d["nnn"])["state"] == \
        "AWAITING_MANUAL_CLEAR"


def test_rotating_stop_respawns(cc_home, capsys, tmp_path, monkeypatch):
    # Orphan recovery: state is ROTATING (e.g. rotator crashed) and a Stop
    # arrives again -> spawn again; the rotation lock kills the duplicate.
    d = _verified_stop_setup(cc_home, tmp_path)
    state.transition(d["key"], d["nnn"], "ROTATING", rotation_id="old-rid")
    monkeypatch.setenv("TMUX_PANE", "%42")
    spawns = []
    monkeypatch.setattr(stop.rotator, "spawn",
                        lambda sid, pane, rid: (spawns.append((sid, pane, rid))
                                                 or 777))
    run_hook("Stop", payload("Stop", d["repo"]))
    rec = state.load_state(d["key"], d["nnn"])
    assert rec["state"] == "ROTATING"
    assert rec["rotation_id"] != "old-rid"  # fresh id for the retry
    assert len(spawns) == 1


def test_spawn_failure_falls_back_to_amcl(cc_home, capsys, tmp_path, monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%42")

    def boom(sid, pane, rid):
        raise stop.rotator.RotatorSpawnError("no exec")

    monkeypatch.setattr(stop.rotator, "spawn", boom)
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(d["key"], d["nnn"])["state"] == \
        "AWAITING_MANUAL_CLEAR"


def test_rotating_transition_race_is_swallowed(cc_home, capsys, tmp_path, monkeypatch):
    # Claim raced the Stop to ROTATED between the load and the transition:
    # the hook must not crash (the spawned rotator self-exits on re-read).
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(stop.rotator, "spawn", lambda s, p, r: 1)

    real_transition = stop.state.transition

    def racing_transition(key, nnn, new_state, **fields):
        if new_state == "ROTATING":
            raise stop.state.InvalidTransitionError("raced")
        return real_transition(key, nnn, new_state, **fields)

    monkeypatch.setattr(stop.state, "transition", racing_transition)
    out = run_hook("Stop", payload("Stop", d["repo"]))
    assert out == 0
    assert capsys.readouterr().out == ""
    assert "raced" in (paths.logs_dir() / "errors.log").read_text()


def test_stop_finalize_failure_pushes_row8(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    repo, key, nnn = _setup(cc_home, tmp_path, "HANDOFF_REQUESTED")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _valid_draft(), encoding="utf-8")
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")

    def boom(*a, **kw):
        raise checkpoint.CheckpointError("verify: checksum mismatch")
    monkeypatch.setattr(checkpoint, "finalize", boom)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body)) or True)
    run_hook("Stop", payload("Stop", repo))
    assert len(sent) == 1
    title, prio, body = sent[0]
    assert title == "✗ cc-continuity: checkpoint finalization failed"
    assert prio == "urgent"
    assert (f"proj=spike-repo {nnn:03d} "
            "detail=verify: checksum mismatch") in body


# --- paneless self-heal (no $TMUX_PANE, PRD §9 Stop) --------------------------


def _paneless_fake(monkeypatch, outcome, info):
    calls = []

    def fake(key, nnn, sid, cfg, rid, **kw):
        calls.append((sid, rid))
        return outcome, info

    monkeypatch.setattr(stop.rotator, "spawn_successor_detached", fake)
    return calls


def test_paneless_stop_self_heal_completed(cc_home, capsys, tmp_path,
                                           monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path)  # CV + real checkpoint
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _paneless_fake(monkeypatch, "completed",
                           {"name": "spike-repo-002",
                            "tmux_session": "cc-spike-repo-ab12"})
    run_hook("Stop", payload("Stop", d["repo"]))
    out = stdout_json(capsys)
    assert out["systemMessage"].startswith("Rotated to spike-repo-002")
    assert "tmux attach -t cc-spike-repo-ab12" in out["systemMessage"]
    rec = state.load_state(d["key"], d["nnn"])
    assert rec["state"] == "ROTATING"
    assert rec["rotation_id"] == calls[0][1]  # the fresh id the hook made


def test_paneless_stop_failed_falls_back_to_amcl(cc_home, capsys, tmp_path,
                                                  monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _paneless_fake(monkeypatch, "failed", None)
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(d["key"], d["nnn"])["state"] == \
        "AWAITING_MANUAL_CLEAR"


def test_paneless_stop_deferred_is_silent(cc_home, capsys, tmp_path,
                                          monkeypatch):
    # The real spawn rolls the state back to CV itself; with the fake it
    # stays ROTATING and the next Stop retries.
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _paneless_fake(monkeypatch, "deferred",
                   {"name": "spike-repo-002", "tmux_session": "cc-x"})
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) is None
    assert state.load_state(d["key"], d["nnn"])["state"] == "ROTATING"


def test_paneless_stop_exited_is_silent(cc_home, capsys, tmp_path, monkeypatch):
    # Another rotator owns the rotation (or the state moved on): nothing
    # to say, nothing to do.
    d = _verified_stop_setup(cc_home, tmp_path)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _paneless_fake(monkeypatch, "exited", None)
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) is None
    assert state.load_state(d["key"], d["nnn"])["state"] == "ROTATING"


def test_paneless_full_auto_disabled_keeps_amcl(cc_home, capsys, tmp_path,
                                                 monkeypatch):
    d = _verified_stop_setup(cc_home, tmp_path)
    (d["repo"] / ".claude").mkdir(exist_ok=True)
    (d["repo"] / ".claude" / "continuity.json").write_text(
        json.dumps({"full_auto": False}))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    run_hook("Stop", payload("Stop", d["repo"]))
    assert stdout_json(capsys) == {"systemMessage": AMCL}
    assert state.load_state(d["key"], d["nnn"])["state"] == \
        "AWAITING_MANUAL_CLEAR"


def test_stop_spawn_failure_pushes_row8(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify, rotator
    repo, key, nnn = _setup(cc_home, tmp_path, "HANDOFF_REQUESTED")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _valid_draft(), encoding="utf-8")
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")

    def fake_finalize(*a, **kw):
        # the real finalize walks CW -> CV internally; the stub must too,
        # or the AMCL fallback below transitions from the wrong state
        state.transition(key, nnn, "CHECKPOINT_VERIFIED")
        return ("cp-001", False)
    monkeypatch.setattr(checkpoint, "finalize", fake_finalize)

    def bad_spawn(*a, **kw):
        raise rotator.RotatorSpawnError("tmux: command not found")
    monkeypatch.setattr(rotator, "spawn", bad_spawn)
    monkeypatch.setenv("TMUX_PANE", "%9")
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body)) or True)
    run_hook("Stop", payload("Stop", repo))
    rows8 = [(t, p, b) for (t, p, b) in sent
             if t == "✗ cc-continuity: rotator spawn failed"]
    assert len(rows8) == 1
    assert rows8[0][1] == "urgent"
    assert "detail=tmux: command not found" in rows8[0][2]
    # spawn failure still falls back to assisted AMCL (P9-D7: push never
    # breaks the loop)
    assert state.load_state(key, nnn)["state"] == "AWAITING_MANUAL_CLEAR"
