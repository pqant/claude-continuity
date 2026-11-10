"""hooks/pre_tool_use.py: hard ceiling (PRD §9, spec §6.3).

Ratio gate: ratio >= hard with a pre-handoff state -> walk to
HANDOFF_REQUESTED, let the in-flight call through and inject the C.2
handoff instruction as neutral additionalContext (CC renders denials as
red "Error:" lines; routine rotation is not an error - issue #25).
State gate: handoff-requested states deny C.3 (only after the model saw
the guidance), handoff-accepted states deny C.9, always with the same
allowlist. Silent allow otherwise.
"""

import pytest

from cc_continuity import ledger, paths, state

from hookutil import (draft_path_for, make_repo, payload, register_session,
                      run_hook, set_metric, stdout_json, walk_to)


def _setup(cc_home, tmp_path, s=None, ratio=None, stale=False):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    if s:
        walk_to(key, nnn, s)
    if ratio is not None:
        set_metric(ratio, stale=stale)
    return repo, key, nnn


def _pretool(repo, tool, tool_input=None):
    return run_hook("PreToolUse", payload("PreToolUse", repo, tool_name=tool,
                                          tool_input=tool_input or {}))


def _reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def _system(out):
    return out.get("systemMessage", "")


def _gates(key, nnn):
    return [e for e in ledger.read(paths.session_dir(key, nnn))
            if e.get("type") == "gate"]


def test_below_hard_allows_silently(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.60)
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    assert stdout_json(capsys) is None
    assert state.load_state(key, nnn)["state"] == "ACTIVE"


def test_hard_ratio_crossing_guides_without_denying(cc_home, capsys, tmp_path):
    # Routine crossing (issue #25): CC renders denials as red "Error:"
    # lines, but rotation is routine - the in-flight call passes and the
    # handoff instruction arrives as neutral additionalContext.
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING", ratio=0.73)
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "Rotation pending" in out["hookSpecificOutput"]["additionalContext"]
    assert "handoff.draft.md" in out["hookSpecificOutput"]["additionalContext"]
    assert "cc-continuity" in _system(out)
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_second_work_tool_after_crossing_denies(cc_home, capsys, tmp_path):
    # Only AFTER the model saw the guidance may a work tool be denied.
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING", ratio=0.73)
    _pretool(repo, "Bash", {"command": "rm -rf x"})  # crossing: allow + guide
    stdout_json(capsys)
    _pretool(repo, "Bash", {"command": "pytest"})
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hard limit" in _system(out)


def test_handoff_requested_deny_routes_guidance_to_system_message(cc_home,
                                                                   capsys,
                                                                   tmp_path):
    # A post-guidance denial renders its full guidance as an informational
    # systemMessage, not only as the red error in the deny reason.
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    out = stdout_json(capsys)
    assert "hard limit" in _system(out)
    assert "Allowed now" in _system(out)


def test_handoff_requested_deny_reason_omits_allowed_tools_guidance(cc_home,
                                                                     capsys,
                                                                     tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    out = stdout_json(capsys)
    assert "Allowed now" not in _reason(out)
    assert "handoff.draft.md" in _reason(out)
    assert "end your turn" in _reason(out)


def test_handoff_accepted_deny_routes_guidance_to_system_message(cc_home,
                                                                 capsys,
                                                                 tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_WRITING")
    _pretool(repo, "Bash", {"command": "pytest"})
    out = stdout_json(capsys)
    assert "Do not start any more work" in _system(out)


def test_handoff_accepted_deny_reason_is_brief(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_WRITING")
    _pretool(repo, "Bash", {"command": "pytest"})
    out = stdout_json(capsys)
    assert "handoff accepted" in _reason(out)
    assert "Do not start any more work" not in _reason(out)


def test_hard_ratio_walks_even_when_tool_allowed(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.75)
    _pretool(repo, "Read", {"file_path": "/x"})
    # The allowlisted call also receives the crossing guidance.
    out = stdout_json(capsys)
    assert "Rotation pending" in out["hookSpecificOutput"]["additionalContext"]
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_stale_metric_allows_and_logs(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING",
                            ratio=0.9, stale=True)
    _pretool(repo, "Bash", {"command": "rm x"})
    assert stdout_json(capsys) is None
    assert state.load_state(key, nnn)["state"] == "ROTATION_PENDING"
    log = (paths.continuity_home() / "logs" / "events.log").read_text()
    assert "METRIC_STALE" in log


def test_handoff_requested_denies_work_tool_without_metric(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    _pretool(repo, "Bash", {"command": "pytest"})
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "handoff.draft.md" in _reason(out)


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "/x"}),
    ("Glob", {"pattern": "*.py"}),
    ("Grep", {"pattern": "x"}),
    ("TaskUpdate", {"taskId": "1", "status": "in_progress"}),
    ("Bash", {"command": "git log -1"}),
    ("Bash", {"command": "cc-continuity status"}),
])
def test_handoff_requested_allowlist_passes(cc_home, capsys, tmp_path,
                                            tool, tool_input):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    _pretool(repo, tool, tool_input)
    assert stdout_json(capsys) is None


def test_handoff_requested_allows_draft_write_only(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    draft = str(draft_path_for(key, nnn))
    _pretool(repo, "Write", {"file_path": draft, "content": "x"})
    assert stdout_json(capsys) is None
    _pretool(repo, "Write", {"file_path": "/elsewhere.py", "content": "x"})
    assert stdout_json(capsys)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("s", ["CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED",
                               "ROTATING"])
def test_handoff_accepted_window_denies_with_c9(cc_home, capsys, tmp_path, s):
    repo, key, nnn = _setup(cc_home, tmp_path, s=s)
    _pretool(repo, "Bash", {"command": "pytest"})
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "handoff accepted" in _reason(out)
    _pretool(repo, "Read", {"file_path": "/x"})
    assert stdout_json(capsys) is None  # allowlist still applies (C.9 reason)


def test_awaiting_manual_clear_allows(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="AWAITING_MANUAL_CLEAR")
    _pretool(repo, "Bash", {"command": "pytest"})
    assert stdout_json(capsys) is None


def test_unregistered_session_silent(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    p = payload("PreToolUse", repo, session_id="nope", tool_name="Bash",
                tool_input={"command": "x"})
    assert run_hook("PreToolUse", p) == 0
    assert capsys.readouterr().out == ""


# ------------------------------------------------------- gate ledger (#39)


def test_crossing_records_guide_gate_entry(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="WARNED", ratio=0.80)
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    stdout_json(capsys)
    g = _gates(key, nnn)
    assert len(g) == 1
    e = g[0]
    assert e["action"] == "guide" and e["variant"] == "c2"
    assert e["from"] == "WARNED" and e["to"] == "HANDOFF_REQUESTED"
    assert e["ratio"] == 0.8


def test_denies_record_gate_entries(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING", ratio=0.80)
    _pretool(repo, "Bash", {"command": "rm -rf x"})  # crossing: guide
    stdout_json(capsys)
    _pretool(repo, "Bash", {"command": "pytest"})     # deny C.3
    stdout_json(capsys)
    g = _gates(key, nnn)
    assert [(e["action"], e["variant"], e["from"], e["to"]) for e in g] == [
        ("guide", "c2", "ROTATION_PENDING", "HANDOFF_REQUESTED"),
        ("deny", "c3", "HANDOFF_REQUESTED", "HANDOFF_REQUESTED")]
    assert g[1]["tool"] == "Bash" and "ratio" not in g[1]


def test_allowed_tool_in_deny_state_records_nothing(cc_home, capsys, tmp_path):
    # Only REAL denials are gate decisions: allowlisted calls pass silently.
    repo, key, nnn = _setup(cc_home, tmp_path, s="HANDOFF_REQUESTED")
    _pretool(repo, "Read", {"file_path": "x"})
    assert stdout_json(capsys) is None
    assert _gates(key, nnn) == []


def test_c9_deny_records_gate_entry(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_VERIFIED")
    _pretool(repo, "Bash", {"command": "pytest"})
    stdout_json(capsys)
    g = _gates(key, nnn)
    assert [(e["action"], e["variant"], e["from"], e["to"]) for e in g] == [
        ("deny", "c9", "CHECKPOINT_VERIFIED", "CHECKPOINT_VERIFIED")]
