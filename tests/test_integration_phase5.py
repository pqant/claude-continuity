"""Phase 5 end-to-end chain without a model (spec §8/§9): warn ->
pending -> hard (PreToolUse ceiling) -> deny -> draft -> C.9 -> Stop ->
finalized checkpoint -> AMCL. Locks the cross-hook sequence; a failure
here is an integration regression (all parts land in Tasks 4-9)."""

from cc_continuity import config, ledger, paths, state, validate
from cc_continuity.hooks import stop
from hookutil import (make_repo, payload, register_session, run_hook,
                      set_metric, stdout_json)


def _draft_text():
    bodies = [  # all 9 headings, template order; sections 1/2/4 >= 20 chars
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


def test_full_threshold_to_checkpoint_chain(cc_home, capsys, tmp_path,
                                             monkeypatch):
    # No $TMUX_PANE here, so the Stop reaches the paneless self-heal
    # branch: make the spawn fail (tests must never launch a real claude
    # in a real tmux session) -> assisted AMCL fallback.
    monkeypatch.setattr(stop.rotator, "spawn_successor_detached",
                        lambda *a, **kw: ("failed", None))
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert state.load_state(key, nnn)["state"] == "ACTIVE"  # Task 9 (D3)
    draft = paths.session_dir(key, nnn) / "handoff.draft.md"

    def ptu(tool="Read", tool_input=None):
        run_hook("PostToolUse", payload("PostToolUse", repo, tool_name=tool,
                                        tool_input=tool_input or {}))
        return stdout_json(capsys)

    def pretool(tool, tool_input=None):
        run_hook("PreToolUse", payload("PreToolUse", repo, tool_name=tool,
                                       tool_input=tool_input or {}))
        return stdout_json(capsys)

    set_metric(0.50)
    assert ptu() is None
    assert state.load_state(key, nnn)["state"] == "ACTIVE"

    set_metric(0.56)  # >= warn_ratio 0.55
    out = ptu()
    assert "rotation will be needed soon" in out["hookSpecificOutput"]["additionalContext"]
    assert state.load_state(key, nnn)["state"] == "WARNED"

    set_metric(0.63)  # >= rotate_ratio 0.62
    out = ptu()
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Rotation pending" in ctx and str(draft) in ctx
    rec = state.load_state(key, nnn)
    assert rec["state"] == "ROTATION_PENDING" and rec["ratio_at_pending"] == 0.63

    assert pretool("Bash", {"command": "echo work"}) is None  # < hard: allowed

    set_metric(0.73)  # >= hard_ratio 0.72
    out = pretool("Bash", {"command": "rm -rf /tmp/whatever"})
    # The PreToolUse ratio gate (spec 6.3) walks RP -> HR, then denies.
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hard limit" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"
    assert pretool("Read", {"file_path": "/x"}) is None  # allowlist passes

    draft.write_text(_draft_text(), encoding="utf-8")
    out = ptu("Write", {"file_path": str(draft), "content": "..."})
    assert "handoff accepted" in out["hookSpecificOutput"]["additionalContext"]
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_WRITING"

    out = pretool("Edit", {"file_path": str(repo / "a.txt")})
    assert "handoff accepted" in out["hookSpecificOutput"]["permissionDecisionReason"]

    run_hook("Stop", payload("Stop", repo, stop_hook_active=False))
    assert stdout_json(capsys) == {
        "systemMessage": "Checkpoint ready. Type /clear to continue automatically."}
    rec = state.load_state(key, nnn)
    assert rec["state"] == "AWAITING_MANUAL_CLEAR" and rec["degraded"] is False

    cp = paths.session_dir(key, nnn) / "checkpoint"
    for name in ("handoff.md", "state.json", "snapshot.json", "ledger.jsonl",
                 "SHA256SUMS"):
        assert (cp / name).is_file()
    status, details = validate.validate_checkpoint(cp,
                                                   config.load_config(cwd=repo),
                                                   root=repo)
    assert status == "OK"

    walk = [e["to"] for e in ledger.read(paths.session_dir(key, nnn))
            if e["type"] == "state"]
    assert walk == ["WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                    "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED",
                    "ROTATING", "CHECKPOINT_VERIFIED",
                    "AWAITING_MANUAL_CLEAR"]
