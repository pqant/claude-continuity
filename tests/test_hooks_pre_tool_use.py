"""hooks/pre_tool_use.py: hard ceiling (PRD §9, spec §6.3).

Ratio gate: ratio >= hard with a pre-handoff state -> walk to
HANDOFF_REQUESTED, then deny. State gate: pre-checkpoint states deny C.3,
handoff-accepted states deny C.9, always with the same allowlist. Silent
allow otherwise.
"""

import pytest

from cc_continuity import paths, state

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


def test_below_hard_allows_silently(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.60)
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    assert stdout_json(capsys) is None
    assert state.load_state(key, nnn)["state"] == "ACTIVE"


def test_hard_ratio_walks_to_handoff_and_denies(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING", ratio=0.73)
    _pretool(repo, "Bash", {"command": "rm -rf x"})
    out = stdout_json(capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hard limit" in _reason(out)
    assert state.load_state(key, nnn)["state"] == "HANDOFF_REQUESTED"


def test_hard_ratio_walks_even_when_tool_allowed(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ACTIVE", ratio=0.75)
    _pretool(repo, "Read", {"file_path": "/x"})
    assert stdout_json(capsys) is None  # Read passes the allowlist
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
