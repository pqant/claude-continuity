"""cli.py: entry point, --version, dispatch (implemented + unimplemented).

Phase 2: ``statusline`` + ``status``. Phase 3: ``hook <event>`` + ``history``.
"""

import argparse
import io
import json
import os
import sys
import time

import pytest

from cc_continuity import __version__, cli, ledger, paths, rotator, state

from hookutil import SID, make_repo, payload, register_session, run_hook


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_exits_2_with_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


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


# ------------------------------------------------------------- Phase 4 cmds

import os as _os
import subprocess as _subprocess
import sys as _sys
from pathlib import Path as _Path

REPO_ROOT = _Path(__file__).resolve().parent.parent

DRAFT_ORDER = [
    "Objective", "Current Task and Exact Stopping Point", "Completed This Session",
    "Next Atomic Action", "Remaining Plan", "Decisions and Rationale",
    "Rejected Approaches — Do Not Repeat",
    "Open Problems, Risks, Questions for the User", "Runtime Environment Notes",
]
DRAFT_BODIES = {
    "Objective": "Ship the checkpoint engine for cc-continuity this session.",
    "Current Task and Exact Stopping Point": (
        "Stopped after implementing finalize; tests green."),
    "Completed This Session": "- Wrote finalize",
    "Next Atomic Action": "Edit `a.txt` and run the quick check.",
    "Remaining Plan": "1. CLI",
    "Decisions and Rationale": "Fault injection for step tests.",
    "Rejected Approaches — Do Not Repeat": "Rejected model-written snapshots.",
    "Open Problems, Risks, Questions for the User": "Watch the gitsnap flake.",
    "Runtime Environment Notes": "Local vLLM, effort low.",
}


def _draft_text(omit=()):
    return "\n".join(f"## {t}\n{DRAFT_BODIES[t]}\n"
                     for t in DRAFT_ORDER if t not in omit)


def _run_cli(*args, cwd, env_extra=None):
    env = {**_os.environ, "PYTHONPATH": str(REPO_ROOT),
           **(env_extra or {})}
    return _subprocess.run(
        [_sys.executable, "-m", "cc_continuity", *args],
        cwd=cwd, env=env, capture_output=True, text=True)


def _registered(tmp_path):
    from hookutil import SID, make_repo, register_session
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    return repo, key, nnn, SID


def test_cli_validate_draft_ok(cc_home, tmp_path, capsys):
    f = tmp_path / "draft.md"
    f.write_text(_draft_text(), encoding="utf-8")
    assert cli.main(["validate-draft", str(f)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("DRAFT OK") and "redacted" in out


def test_cli_validate_draft_invalid(cc_home, tmp_path, capsys):
    f = tmp_path / "draft.md"
    f.write_text(_draft_text(omit=("Remaining Plan",)), encoding="utf-8")
    assert cli.main(["validate-draft", str(f)]) == 1
    out = capsys.readouterr().out
    assert "handoff draft invalid" in out
    assert "missing heading: Remaining Plan" in out


def test_cli_checkpoint_e2e(cc_home, tmp_path, capsys):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import paths as _paths
    (_paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft_text(), encoding="utf-8")
    r = _run_cli("checkpoint", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert f"CHECKPOINT cp-{nnn:03d}" in r.stdout
    cp = _paths.session_dir(key, nnn) / "checkpoint"
    assert (cp / "handoff.md").is_file() and (cp / "SHA256SUMS").is_file()
    from cc_continuity import state as _state
    assert _state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"


def test_cli_checkpoint_degraded_without_draft(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    r = _run_cli("checkpoint", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "DEGRADED" in r.stdout


def test_cli_checkpoint_no_session(cc_home, tmp_path, capsys):
    repo = tmp_path / "empty"
    repo.mkdir()
    # empty value = unset (see _resolve_session); guard against the
    # developer's own env leaking a live session id
    r = _run_cli("checkpoint", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": ""})
    assert r.returncode == 1
    assert "No registered session" in r.stderr


def test_cli_validate_e2e(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import paths as _paths
    (_paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft_text(), encoding="utf-8")
    r = _run_cli("checkpoint", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    cp = _paths.session_dir(key, nnn) / "checkpoint"
    r = _run_cli("validate", "--checkpoint", str(cp), cwd=repo)
    assert r.returncode == 0 and "VALIDATION: OK" in r.stdout, r.stdout + r.stderr
    (repo / "b.txt").write_text("b\n")
    r = _run_cli("validate", "--checkpoint", str(cp), cwd=repo)
    assert r.returncode == 1 and "VALIDATION: MISMATCH" in r.stdout
    assert "b.txt" in r.stdout


# ------------------------------------------------- Phase 5: rotation cmds


def test_cli_request_rotation_walks_to_pending(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import state as _state, statusline as _statusline
    import time
    _statusline.write_metric({"session_id": sid, "ts": time.time(),
                              "used_tokens": 15729, "window_tokens": 262144,
                              "ratio": 0.60})
    r = _run_cli("request-rotation", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "Rotation pending" in r.stdout
    rec = _state.load_state(key, nnn)
    assert rec["state"] == "ROTATION_PENDING"
    assert rec["ratio_at_pending"] == 0.60


def test_cli_request_rotation_idempotent(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import state as _state
    _state.transition(key, nnn, "ROTATION_PENDING")  # register starts ACTIVE
    r = _run_cli("request-rotation", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "Already ROTATION_PENDING" in r.stdout


def test_cli_request_rotation_past_pending(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import state as _state
    for s in ("ROTATION_PENDING", "HANDOFF_REQUESTED"):
        _state.transition(key, nnn, s)
    r = _run_cli("request-rotation", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "Already past ROTATION_PENDING" in r.stdout


def test_cli_request_rotation_no_session(cc_home, tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    r = _run_cli("request-rotation", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": ""})
    assert r.returncode == 1
    assert "No registered session" in r.stderr


def test_cli_off_on_toggle(cc_home, tmp_path):
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import state as _state
    r = _run_cli("off", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "off=true" in r.stdout
    assert _state.load_state(key, nnn)["off"] is True
    r = _run_cli("on", cwd=repo, env_extra={"CC_CONTINUITY_SESSION": sid})
    assert r.returncode == 0, r.stderr
    assert "off=false" in r.stdout
    assert _state.load_state(key, nnn)["off"] is False


def test_post_tool_use_noop_when_session_off(cc_home, capsys, tmp_path):
    """Spec §7: off wins everywhere — the ceiling itself is skipped."""
    repo, key, nnn, sid = _registered(tmp_path)
    from cc_continuity import state as _state, statusline as _statusline
    import time
    _state.transition(key, nnn, "ACTIVE")
    _state.update_fields(key, nnn, off=True)
    _statusline.write_metric({"session_id": sid, "ts": time.time(),
                              "used_tokens": 235929, "window_tokens": 262144,
                              "ratio": 0.90})
    run_hook("PostToolUse", payload("PostToolUse", repo, tool_name="Bash",
                                    tool_input={"command": "echo x"}))
    assert capsys.readouterr().out == ""
    rec = _state.load_state(key, nnn)
    assert rec["state"] == "ACTIVE" and rec["tool_calls"] == 0


# --- rotate (Phase 6, PRD §11/§16) ---------------------------------------------
# Setup copied from tests/test_rotator.py (test modules stay independent).

_FAKE_TMUX = """#!/bin/sh
printf '%s\\n' "---" "$@" >> "${FAKE_TMUX_LOG:?}"
exit 0
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(_FAKE_TMUX)
    os.chmod(bin_path, 0o755)
    log = tmp_path / "tmux-args.log"
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    return log


_DRAFT = "\n".join(f"## {h}\n{b}\n" for h, b in [
    ("Objective", "Implement the feature end to end in the sandbox repo."),
    ("Current Task and Exact Stopping Point",
     "Just finished the edit; the test run has not happened yet."),
    ("Completed This Session", "- Added the module and its imports."),
    ("Next Atomic Action",
     "Run `pytest -q` and fix any failure that appears."),
    ("Remaining Plan", "1. Tests. 2. Docs."),
    ("Decisions and Rationale", "Kept stdlib only, per the constraint."),
    ("Rejected Approaches — Do Not Repeat", "No third-party dependencies."),
    ("Open Problems, Risks, Questions for the User",
     "None known at the moment."),
    ("Runtime Environment Notes", "No running servers or ports."),
])


def _rotating(cc_home, tmp_path) -> dict:
    """Session 001 in ROTATING with a real checkpoint (post-Stop shape)."""
    from cc_continuity import checkpoint, config, project

    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(_DRAFT)
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    rid = "rid-test"
    state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    state.transition(key, nnn, "ROTATING", rotation_id=rid)
    cfg = dict(config.DEFAULTS)
    cfg["settle_ms"] = 0
    return {"repo": repo, "key": key, "nnn": nnn, "rid": rid, "cfg": cfg,
            "sid": SID}


def test_cli_rotate_runs_rotator(cc_home, tmp_path, fake_tmux, monkeypatch,
                                 capsys):
    d = _rotating(cc_home, tmp_path)
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    rc = cli.main(["rotate", "--session", d["sid"], "--pane", "%9",
                   "--rotation-id", d["rid"]])
    # dead pane (no hooks) -> full failure path
    assert rc == 1
    assert state.load_state(d["key"], d["nnn"])["state"] == "CHECKPOINT_VERIFIED"


def test_cli_rotate_unknown_session(cc_home, capsys):
    assert cli.main(["rotate", "--session", "nope", "--pane", "%1",
                     "--rotation-id", "r"]) == 1
    assert "No registered session" in capsys.readouterr().err


def test_configure_ntfy_roundtrip(cc_home):
    assert cli.main(["configure", "--ntfy",
                     "https://ntfy.sh/example-topic"]) == 0
    cfg = json.loads((cc_home / "config.json").read_text(encoding="utf-8"))
    assert cfg["notify_endpoint"] == "https://ntfy.sh/example-topic"
    assert cfg["notify_enabled"] is True
    assert cli.main(["configure", "--ntfy", "off"]) == 0
    cfg = json.loads((cc_home / "config.json").read_text(encoding="utf-8"))
    assert cfg["notify_endpoint"] == ""
    assert cfg["notify_enabled"] is False


def test_configure_unattended_and_combined(cc_home):
    assert cli.main(["configure", "--ntfy", "https://ntfy.sh/t",
                     "--unattended"]) == 0
    cfg = json.loads((cc_home / "config.json").read_text(encoding="utf-8"))
    assert cfg["unattended"] is True
    assert cfg["notify_enabled"] is True


def test_configure_invalid_endpoint_writes_nothing(cc_home, capsys):
    assert cli.main(["configure", "--ntfy", "nope"]) == 1
    assert not (cc_home / "config.json").exists()
    assert "invalid ntfy endpoint" in capsys.readouterr().err


def test_configure_without_flags_is_error(cc_home, capsys):
    assert cli.main(["configure"]) == 1
    assert "nothing to change" in capsys.readouterr().err


def test_signal_needs_human_pushes_and_records(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")
    key, nnn = register_session(repo)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body, kw.get("wait"))) or True)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)  # primary resolution
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "needs-human", "drop production db?"]) == 0
    assert len(sent) == 1
    title, prio, body, wait = sent[0]
    assert title == "🛑 cc-continuity: BLOCKED — waiting for you"
    assert prio == "urgent"
    assert f"proj=spike-repo {nnn:03d} reason=drop production db?" in body
    assert wait is True  # CLI path: wait=True (P9-P4)
    last = ledger.read(paths.session_dir(key, nnn))[-1]
    last.pop("ts")  # ledger.append always adds ts
    assert last == {"type": "needs_human",
                    "reason": "drop production db?"}


def test_signal_done_pushes_and_records(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")
    key, nnn = register_session(repo)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, body)) or True)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)  # primary resolution
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "done", "all 14 tasks green"]) == 0
    assert sent == [("✅ cc-continuity: task complete",
                     "proj=spike-repo summary=all 14 tasks green")]
    last = ledger.read(paths.session_dir(key, nnn))[-1]
    last.pop("ts")  # ledger.append always adds ts
    assert last == {"type": "task_done", "summary": "all 14 tasks green"}


def test_signal_without_session_exits_1_no_push(cc_home, tmp_path,
                                                monkeypatch, capsys):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda *a, **kw: sent.append(1) or True)
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "done", "x"]) == 1
    assert "No registered session" in capsys.readouterr().err
    assert sent == []

# --- Phase 10: watchdog subcommand group + configure flags ------------------


def test_watchdog_subparser_smoke():
    for action in ("install", "uninstall", "start", "stop", "status",
                   "run"):
        ns = cli.build_parser().parse_args(["watchdog", action])
        assert ns.command == "watchdog"
        assert ns.watchdog_command == action


def _wd_ns(action):
    return argparse.Namespace(command="watchdog",
                              watchdog_command=action)


def _fake_runner():
    calls = []

    class R:
        returncode = 0
        stdout = "state = running\n"
        stderr = ""

    def runner(cmd, **kw):
        calls.append(cmd)
        return R()

    return runner, calls


def test_watchdog_install_refuses_when_disabled(cc_home, tmp_path,
                                                capsys):
    (cc_home / "config.json").write_text("{}", encoding="utf-8")
    runner, calls = _fake_runner()
    rc = cli.cmd_watchdog(_wd_ns("install"), home=tmp_path, runner=runner)
    assert rc == 1
    assert "watchdog_enabled is false" in capsys.readouterr().err
    assert calls == []  # no launchctl, no plist


def test_watchdog_install_when_enabled(cc_home, tmp_path, capsys,
                                       monkeypatch):
    from cc_continuity import watchdogd
    (cc_home / "config.json").write_text(
        json.dumps({"watchdog_enabled": True}), encoding="utf-8")
    runner, calls = _fake_runner()
    monkeypatch.setattr(watchdogd.shutil, "which",
                        lambda name: "/bin/cc-continuity")
    assert cli.cmd_watchdog(_wd_ns("install"), home=tmp_path,
                            runner=runner) == 0
    plist = tmp_path / "Library" / "LaunchAgents" \
        / "com.claude-continuity.watchdog.plist"
    assert plist.is_file()
    assert calls[0][:3] == ["launchctl", "bootstrap",
                            f"gui/{os.getuid()}"]
    assert "watchdog installed" in capsys.readouterr().out


def test_watchdog_uninstall(cc_home, tmp_path, capsys, monkeypatch):
    from cc_continuity import watchdogd
    (cc_home / "config.json").write_text(
        json.dumps({"watchdog_enabled": True}), encoding="utf-8")
    runner, calls = _fake_runner()
    monkeypatch.setattr(watchdogd.shutil, "which",
                        lambda name: "/bin/cc-continuity")
    cli.cmd_watchdog(_wd_ns("install"), home=tmp_path, runner=runner)
    assert cli.cmd_watchdog(_wd_ns("uninstall"), home=tmp_path,
                            runner=runner) == 0
    assert not (tmp_path / "Library" / "LaunchAgents"
                / "com.claude-continuity.watchdog.plist").exists()
    assert calls[-1][:3] == ["launchctl", "bootout",
                             f"gui/{os.getuid()}/"
                             "com.claude-continuity.watchdog"]


def test_watchdog_status_not_loaded(cc_home, tmp_path, capsys):
    runner, _ = _fake_runner()

    class NotLoaded:
        returncode = 1
        stdout = ""
        stderr = ""

    assert cli.cmd_watchdog(_wd_ns("status"), home=tmp_path,
                            runner=lambda cmd, **kw: NotLoaded()) == 0
    assert "not loaded" in capsys.readouterr().out


def test_configure_watchdog_flags(cc_home):
    assert cli.main(["configure", "--watchdog-enabled", "true",
                     "--watchdog-health", "http://127.0.0.1:18000/health"
                     ]) == 0
    cfg = json.loads((cc_home / "config.json").read_text(encoding="utf-8"))
    assert cfg["watchdog_enabled"] is True
    assert cfg["watchdog_health_url"] == "http://127.0.0.1:18000/health"


def test_configure_watchdog_health_off_and_disabled(cc_home):
    assert cli.main(["configure", "--watchdog-health", "off",
                     "--watchdog-enabled", "false"]) == 0
    cfg = json.loads((cc_home / "config.json").read_text(encoding="utf-8"))
    assert cfg["watchdog_health_url"] == ""
    assert cfg["watchdog_enabled"] is False


def test_configure_watchdog_health_invalid_writes_nothing(cc_home,
                                                          capsys):
    assert cli.main(["configure", "--watchdog-health", "not-a-url"]) == 1
    assert not (cc_home / "config.json").exists()
    assert "invalid health URL" in capsys.readouterr().err
