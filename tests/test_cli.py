"""cli.py: entry point, --version, dispatch (implemented + unimplemented).

Phase 2: ``statusline`` + ``status``. Phase 3: ``hook <event>`` + ``history``.
"""

import io
import json
import sys

import pytest

from cc_continuity import __version__, cli, ledger, paths

from hookutil import SID, make_repo, payload, register_session, run_hook


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_exits_2_with_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "command",
    ["rotate", "checkpoint", "validate", "doctor",
     "install", "uninstall"],
)
def test_unimplemented_subcommand_reports_phase(command, capsys):
    assert cli.main([command]) == 2
    err = capsys.readouterr().err
    assert command in err
    assert "Phase" in err


# -------------------------------------------------------------------- hook


def test_hook_requires_event(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["hook"])
    assert exc.value.code == 2
    assert "event" in capsys.readouterr().err


def test_hook_dispatches_to_handler(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = payload("UserPromptSubmit", repo, prompt="via cli")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
    assert cli.main(["hook", "UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""  # ledger hooks print nothing
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "user_prompt"
    assert entry["text"] == "via cli"


def test_hook_unknown_event_exits_0_silently(cc_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli.main(["hook", "PostCompact"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_malformed_stdin_exits_0_silently(cc_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{broken"))
    assert cli.main(["hook", "UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_session_start_outputs_notice_json(cc_home, capsys, tmp_path,
                                                monkeypatch):
    """Documented JSON on stdout (C.5) passes through the CLI unscathed."""
    repo = make_repo(tmp_path)
    run_hook("SessionStart", payload("SessionStart", repo, source="startup"))
    from cc_continuity import project

    key = project.project_key_for_root(project.resolve_root(repo))
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text())
    pref["latest_checkpoint"] = {
        "cp_id": "cp-001", "created": "2026-09-12T05:00:00+00:00",
        "pending_consumption": True, "handoff_path": "/x/handoff.md",
    }
    pfile.write_text(json.dumps(pref))

    p = payload("SessionStart", repo, source="startup",
                session_id="66666666-6666-6666-6666-666666666666")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
    assert cli.main(["hook", "SessionStart"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "unconsumed checkpoint cp-001" in out["hookSpecificOutput"][
        "additionalContext"]


# ----------------------------------------------------------------- history


def test_history_without_project(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cli.main(["history"]) == 0
    assert "No cc-continuity project" in capsys.readouterr().out


def test_history_lists_session_chain(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    register_session(repo, session_id="22222222-2222-2222-2222-222222222222")
    monkeypatch.chdir(repo)
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert "spike-repo-001" in out
    assert "spike-repo-002" in out


def test_history_n_limits_output(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    register_session(repo, session_id="22222222-2222-2222-2222-222222222222")
    monkeypatch.chdir(repo)
    assert cli.main(["history", "-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "spike-repo-002" in out
    assert "spike-repo-001" not in out
