"""hooks/tool_use.py: PostToolUse + PostToolUseFailure ledger entries (PRD §9).

Threshold evaluation and additionalContext emission are Phase 5 — this phase
only appends: tool name; edit tools → ``file_path``; Bash → redacted command
(≤ 500) + interrupted flag; task tools → task_id/subject/status; failure →
``{type:tool_failure, tool, error_first_line}`` (S1/N1: a failed Bash raises
PostToolUseFailure, NOT PostToolUse).
"""

import pytest

from cc_continuity import ledger, paths, state

from hookutil import (draft_path_for, fixture_payload, make_repo, payload,
                      register_session, run_hook, set_metric, stdout_json,
                      walk_to)


def _last_tool_entry(key, nnn, etype="tool_use"):
    entries = [e for e in ledger.read(paths.session_dir(key, nnn))
               if e["type"] == etype]
    assert entries, "no matching ledger entry"
    return entries[-1]


def test_fixture_taskupdate_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUse", fixture_payload("PostToolUse", repo))
    entry = _last_tool_entry(key, nnn)
    assert entry["tool"] == "TaskUpdate"
    assert entry["task_id"] == "1"
    assert entry["status"] == "completed"


def test_write_tool_records_file_path(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = payload("PostToolUse", repo, tool_name="Write",
                tool_input={"file_path": "/repo/handoff.draft.md", "content": "…"},
                tool_response=None)
    run_hook("PostToolUse", p)
    entry = _last_tool_entry(key, nnn)
    assert entry == {"type": "tool_use", "tool": "Write",
                     "file_path": "/repo/handoff.draft.md", "ts": entry["ts"]}


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_edit_tools_all_record_file_path(cc_home, tmp_path, tool):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUse", payload("PostToolUse", repo, tool_name=tool,
                                    tool_input={"file_path": "/a.py"}))
    assert _last_tool_entry(key, nnn)["file_path"] == "/a.py"


def test_bash_command_redacted_and_truncated(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    command = "echo token=abc " + "y" * 600
    p = payload("PostToolUse", repo, tool_name="Bash",
                tool_input={"command": command, "description": "run"},
                tool_response={"stdout": "", "stderr": "", "interrupted": False,
                               "isImage": False, "noOutputExpected": False})
    run_hook("PostToolUse", p)
    entry = _last_tool_entry(key, nnn)
    assert "token=abc" not in entry["command"]
    assert "[REDACTED]" in entry["command"]
    assert len(entry["command"]) <= 500
    assert "interrupted" not in entry  # false flag is noise


def test_bash_interrupted_flag_recorded(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = payload("PostToolUse", repo, tool_name="Bash",
                tool_input={"command": "sleep 60"},
                tool_response={"stdout": "", "stderr": "", "interrupted": True,
                               "isImage": False, "noOutputExpected": False})
    run_hook("PostToolUse", p)
    entry = _last_tool_entry(key, nnn)
    assert entry["command"] == "sleep 60"
    assert entry["interrupted"] is True


def test_bash_without_tool_response_still_logs_command(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUse", payload("PostToolUse", repo, tool_name="Bash",
                                    tool_input={"command": "ls -la"},
                                    tool_response=None))
    entry = _last_tool_entry(key, nnn)
    assert entry["command"] == "ls -la"


def test_unknown_tool_gets_minimal_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUse", payload("PostToolUse", repo, tool_name="WebFetch",
                                    tool_input={"url": "https://x"},
                                    tool_response="page text"))
    entry = _last_tool_entry(key, nnn)
    assert entry == {"type": "tool_use", "tool": "WebFetch", "ts": entry["ts"]}


def test_fixture_failure_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUseFailure", fixture_payload("PostToolUseFailure", repo))
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert entry["tool"] == "Bash"
    assert entry["error_first_line"] == "Exit code 1"  # S1: "Exit code N\n<line>"


def test_failure_error_multi_line_takes_first_line(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = payload("PostToolUseFailure", repo, tool_name="Bash",
                tool_input={"command": "false"},
                error="Exit code 1\nline two\nline three")
    run_hook("PostToolUseFailure", p)
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert entry["error_first_line"] == "Exit code 1"


def test_failure_without_error_field(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUseFailure",
             payload("PostToolUseFailure", repo, tool_name="Edit",
                     tool_input={"file_path": "/a.py"}))
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert entry == {"type": "tool_failure", "tool": "Edit", "ts": entry["ts"]}


def test_unregistered_session_is_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = payload("PostToolUse", repo, session_id="no-such-session",
                tool_name="Bash", tool_input={"command": "ls"})
    assert run_hook("PostToolUse", p) == 0
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()


# ------------------------------------------------- Phase 4: failure command


def test_failure_bash_records_redacted_command(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUseFailure", payload(
        "PostToolUseFailure", repo, tool_name="Bash",
        tool_input={"command": "export token=abc123 && pytest -q"},
        error="Exit code 1"))
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert entry["command"] == "export [REDACTED] && pytest -q"


def test_failure_bash_command_truncated(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    long_cmd = "echo " + "x" * 600
    run_hook("PostToolUseFailure", payload(
        "PostToolUseFailure", repo, tool_name="Bash",
        tool_input={"command": long_cmd}, error="Exit code 1"))
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert len(entry["command"]) == 500


def test_failure_non_bash_has_no_command(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("PostToolUseFailure", payload(
        "PostToolUseFailure", repo, tool_name="WebFetch",
        tool_input={"url": "https://x"}, error="timeout"))
    entry = _last_tool_entry(key, nnn, etype="tool_failure")
    assert "command" not in entry


# ------------------------------------------------- Phase 5: thresholds


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


def _setup(cc_home, tmp_path, s=None, ratio=None):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    if s:
        walk_to(key, nnn, s)
    if ratio is not None:
        set_metric(ratio)
    return repo, key, nnn


def _ptu(repo, tool="Read", tool_input=None, **extra):
    return run_hook("PostToolUse", payload("PostToolUse", repo,
                                           tool_name=tool,
                                           tool_input=tool_input or {},
                                           **extra))


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"]


def test_below_warn_no_change(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.50)
    _ptu(repo)
    assert stdout_json(capsys) is None
    assert state.load_state(key, nnn)["state"] == "ACTIVE"


def test_warn_crossing_emits_c1_once(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.56)
    _ptu(repo)
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "rotation will be needed soon" in _ctx(out)
    rec = state.load_state(key, nnn)
    assert rec["state"] == "WARNED" and rec["warned"] is True
    _ptu(repo)  # same ratio, state unchanged -> no re-emit
    assert stdout_json(capsys) is None


def test_rotate_crossing_from_active(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.63)
    _ptu(repo)
    ctx = _ctx(stdout_json(capsys))
    assert "Rotation pending" in ctx
    assert str(draft_path_for(key, nnn)) in ctx
    rec = state.load_state(key, nnn)
    assert rec["state"] == "ROTATION_PENDING"
    assert rec["ratio_at_pending"] == 0.63 and rec["warned"] is True


def test_warned_to_pending(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="WARNED", ratio=0.63)
    _ptu(repo)
    assert "Rotation pending" in _ctx(stdout_json(capsys))
    assert state.load_state(key, nnn)["state"] == "ROTATION_PENDING"


def test_hard_jump_from_active_walks_and_emits_c2(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.73)
    _ptu(repo)
    assert "Rotation pending" in _ctx(stdout_json(capsys))
    rec = state.load_state(key, nnn)
    assert rec["state"] == "HANDOFF_REQUESTED"
    walk = [(e["from"], e["to"]) for e in ledger.read(paths.session_dir(key, nnn))
            if e["type"] == "state"]
    # tail: the setup walk_to("ACTIVE") itself is one ledgered transition
    # while sessions still register as BOOTSTRAPPING (Task 9 flips this)
    assert walk[-2:] == [("ACTIVE", "ROTATION_PENDING"),
                         ("ROTATION_PENDING", "HANDOFF_REQUESTED")]


def test_pending_hard_via_post_tool_use_is_silent(cc_home, capsys, tmp_path):
    """Spec 6.2 row 2 is {BOOTSTRAPPING, ACTIVE, WARNED} only — the
    PreToolUse ratio gate (spec 6.3) owns RP -> HR at hard, so a
    PostToolUse at hard in ROTATION_PENDING must NOT walk or re-emit
    (a double walk would double-emit C.2)."""
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING", ratio=0.73)
    _ptu(repo)
    assert stdout_json(capsys) is None
    assert state.load_state(key, nnn)["state"] == "ROTATION_PENDING"


def test_boundary_flag_fires_on_next_event(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING")
    _ptu(repo, "Bash", {"command": "git commit -m x"},
         tool_response={"stdout": "[main] x", "stderr": "", "interrupted": False})
    assert stdout_json(capsys) is None  # the boundary event itself is silent
    assert state.load_state(key, nnn)["boundary_pending"] is True
    _ptu(repo)  # the NEXT PostToolUse consumes the flag
    assert "Rotation pending" in _ctx(stdout_json(capsys))
    rec = state.load_state(key, nnn)
    assert rec["state"] == "HANDOFF_REQUESTED" and rec["boundary_pending"] is False


def test_boundary_requires_pending_state(cc_home, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE")
    _ptu(repo, "Bash", {"command": "git commit -m x"},
         tool_response={"stdout": "ok", "stderr": "", "interrupted": False})
    rec = state.load_state(key, nnn)
    assert rec["state"] == "ACTIVE" and rec["boundary_pending"] is False


def test_test_command_boundary(cc_home, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING")
    _ptu(repo, "Bash", {"command": "pytest -q"},
         tool_response={"stdout": "ok", "stderr": "", "interrupted": False})
    assert state.load_state(key, nnn)["boundary_pending"] is True


def test_taskupdate_completed_boundary(cc_home, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING")
    _ptu(repo, "TaskUpdate", {"taskId": "1", "status": "completed"},
         tool_response={"statusChange": {"from": "in_progress",
                                         "to": "completed"}})
    assert state.load_state(key, nnn)["boundary_pending"] is True


def test_draft_write_valid_moves_to_writing_and_c9(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    dp = draft_path_for(key, nnn)
    dp.write_text(_valid_draft(), encoding="utf-8")
    _ptu(repo, "Write", {"file_path": str(dp), "content": "…"})
    assert "handoff accepted" in _ctx(stdout_json(capsys))
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_WRITING"


def test_draft_write_invalid_returns_c6(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    dp = draft_path_for(key, nnn)
    dp.write_text("## Objective\nshort\n", encoding="utf-8")  # 8 headings missing
    _ptu(repo, "Write", {"file_path": str(dp), "content": "…"})
    ctx = _ctx(stdout_json(capsys))
    assert "handoff draft invalid" in ctx
    assert "missing heading:" in ctx
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_draft_write_other_path_is_not_validated(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    other = repo / "notes.md"
    other.write_text(_valid_draft(), encoding="utf-8")
    _ptu(repo, "Write", {"file_path": str(other), "content": "…"})
    assert stdout_json(capsys) is None  # no C.6/C.9 for a non-draft write
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_c2_reemits_at_most_every_5_calls(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    state.update_fields(key, nnn, tool_calls=1, ctx_emitted_at=1)
    for _ in range(4):  # tool_calls 2..5: 5 - 1 < 5 -> no emit
        _ptu(repo)
        assert stdout_json(capsys) is None
    _ptu(repo)  # tool_calls 6: 6 - 1 >= 5 -> re-emit
    assert "Rotation pending" in _ctx(stdout_json(capsys))


def test_subagent_payload_skips_threshold_logic(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.9)
    _ptu(repo, "Bash", {"command": "ls"}, agent_id="sub-1")
    assert stdout_json(capsys) is None
    rec = state.load_state(key, nnn)
    assert rec["state"] == "ACTIVE" and rec["tool_calls"] == 0
    entries = [e for e in ledger.read(paths.session_dir(key, nnn))
               if e["type"] == "tool_use"]
    assert entries and entries[-1]["tool"] == "Bash"  # ledger unchanged
