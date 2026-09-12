"""hooks/common.py: the PRD §9 hook entry contract.

Never print anything except documented JSON, never break a session (every
internal exception → ``logs/errors.log`` + empty stdout + exit 0), no-op when
disabled (global config, project opt-out, ``CC_CONTINUITY=0``, session off).
"""

import json

import pytest

from cc_continuity import state
from cc_continuity.hooks import common

from hookutil import SID, make_repo, payload


def _ok_handler():
    calls = []

    def handle(ctx):
        calls.append(ctx)
        return None

    return handle, calls


def _home(cc_home):
    return cc_home / "logs" / "errors.log"


# ------------------------------------------------------------------ stdin


def test_unknown_event_is_silent_noop(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    assert common.run_event("PostCompact", json.dumps(payload("PostCompact", repo)), {}) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("event", ["Stop", "PreToolUse", "PreCompact"])
def test_phase56_events_are_silent_noops(cc_home, capsys, tmp_path, event):
    """Registered later (Phases 5–6): must stay inert, not error."""
    repo = make_repo(tmp_path)
    assert common.run_event(event, json.dumps(payload(event, repo)), {}) == 0
    assert capsys.readouterr().out == ""


def test_malformed_json_logs_and_exits_0(cc_home, capsys):
    assert common.run_event("UserPromptSubmit", "{not json", {}) == 0
    assert capsys.readouterr().out == ""
    assert _home(cc_home).is_file()
    assert "UserPromptSubmit" in _home(cc_home).read_text()


def test_non_object_json_is_silently_ignored(cc_home, capsys):
    assert common.run_event("UserPromptSubmit", "[1, 2]", {}) == 0
    assert capsys.readouterr().out == ""


def test_empty_stdin_is_silently_ignored(cc_home, capsys):
    assert common.run_event("UserPromptSubmit", "", {}) == 0
    assert capsys.readouterr().out == ""


def test_handler_receives_parsed_payload_and_cfg(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    p = payload("UserPromptSubmit", repo, prompt="hello")
    common.run_event("UserPromptSubmit", json.dumps(p), {"UserPromptSubmit": handle})
    assert len(calls) == 1
    ctx = calls[0]
    assert ctx.event == "UserPromptSubmit"
    assert ctx.payload["prompt"] == "hello"
    assert ctx.cfg["warn_ratio"] == 0.55  # PRD default reached via merge
    assert ctx.session is None  # unregistered session id


# ---------------------------------------------------------------- disabled


def test_disabled_by_cc_continuity_env(cc_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CC_CONTINUITY", "0")
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": handle}) == 0
    assert calls == []


def test_disabled_by_global_config(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({"enabled": False}))
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": handle}) == 0
    assert calls == []


def test_disabled_by_project_optout_in_payload_cwd(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    cdir = repo / ".claude"
    cdir.mkdir()
    (cdir / "continuity.json").write_text(json.dumps({"enabled": False}))
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": handle}) == 0
    assert calls == []


def test_disabled_by_session_off_flag(cc_home, tmp_path):
    from hookutil import fixture_payload, run_hook

    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    from cc_continuity import project
    key = project.project_key_for_root(project.resolve_root(repo))
    state.update_fields(key, 1, off=True)
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": handle}) == 0
    assert calls == []


# ------------------------------------------------------- exception safety


def test_handler_exception_never_surfaces(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)

    def bad(ctx):
        raise RuntimeError("boom-phase3")

    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": bad}) == 0
    assert capsys.readouterr().out == ""
    errlog = _home(cc_home).read_text()
    assert "boom-phase3" in errlog
    assert "Traceback" in errlog


def test_unregistered_session_gets_none_and_handler_still_runs(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo,
                                               session_id="no-such-session")),
        {"UserPromptSubmit": handle}) == 0
    assert len(calls) == 1
    assert calls[0].session is None


# --------------------------------------------------------------- outputs


def test_handler_dict_is_printed_as_json(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    expected = {"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": "notice"}}

    def h(ctx):
        return expected

    assert common.run_event("SessionStart", json.dumps(payload("SessionStart", repo)),
                            {"SessionStart": h}) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_handler_none_prints_nothing(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    handle, _ = _ok_handler()
    common.run_event("UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
                     {"UserPromptSubmit": handle})
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------- env file (S11)


def test_claude_env_file_gets_session_export(cc_home, monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    env_file = cc_home / "session-env.sh"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    handle, calls = _ok_handler()
    common.run_event("UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
                     {"UserPromptSubmit": handle})
    content = env_file.read_text()
    assert f'export CC_CONTINUITY_SESSION="{SID}"' in content


def test_claude_env_file_absent_is_harmless(cc_home, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    assert common.run_event(
        "UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
        {"UserPromptSubmit": handle}) == 0
    assert len(calls) == 1


def test_env_export_skipped_when_disabled(cc_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(cc_home / "env.sh"))
    monkeypatch.setenv("CC_CONTINUITY", "0")
    repo = make_repo(tmp_path)
    handle, calls = _ok_handler()
    common.run_event("UserPromptSubmit", json.dumps(payload("UserPromptSubmit", repo)),
                     {"UserPromptSubmit": handle})
    assert calls == []
    assert not (cc_home / "env.sh").exists()


# ---------------------------------------------------------------- helpers


def test_log_error_never_raises_on_missing_logs_dir(cc_home):
    from cc_continuity import paths

    paths.ensure_root()  # create the layout, then remove the logs dir
    (cc_home / "logs").rmdir()
    common.log_error("synthetic context")  # must not raise, must recreate
    assert (cc_home / "logs" / "errors.log").is_file()
