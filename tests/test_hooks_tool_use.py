"""hooks/tool_use.py: PostToolUse + PostToolUseFailure ledger entries (PRD §9).

Threshold evaluation and additionalContext emission are Phase 5 — this phase
only appends: tool name; edit tools → ``file_path``; Bash → redacted command
(≤ 500) + interrupted flag; task tools → task_id/subject/status; failure →
``{type:tool_failure, tool, error_first_line}`` (S1/N1: a failed Bash raises
PostToolUseFailure, NOT PostToolUse).
"""

import pytest

from cc_continuity import ledger, paths

from hookutil import fixture_payload, make_repo, payload, register_session, run_hook


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
