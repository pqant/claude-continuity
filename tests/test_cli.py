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

from hookutil import (SID, make_repo, payload, register_session, run_hook,
                      set_metric)


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


# -------------------------------------------------------------------- ratio


def test_ratio_prints_current_project(cc_home, tmp_path, monkeypatch,
                                      capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.774, session_id=SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio"]) == 0
    assert capsys.readouterr().out == "spike-repo 0.774\n"


def test_ratio_no_metrics_exits_1(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio"]) == 1
    assert "no metrics" in capsys.readouterr().err


def test_ratio_explicit_slug_from_elsewhere(cc_home, tmp_path, monkeypatch,
                                            capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.4, session_id=SID)
    elsewhere = make_repo(tmp_path, name="elsewhere-repo")
    monkeypatch.chdir(elsewhere)
    assert cli.main(["ratio", "spike-repo"]) == 0
    assert capsys.readouterr().out == "spike-repo 0.4\n"


def test_ratio_unknown_slug_exits_1(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    monkeypatch.chdir(repo)
    assert cli.main(["ratio", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


# ---------------------------------------------------------- ratio --json


def test_ratio_json_prints_single_object(cc_home, tmp_path, monkeypatch,
                                         capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.774, session_id=SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)  # exactly one JSON object
    assert obj["slug"] == "spike-repo"
    assert obj["session_id"] == SID
    assert obj["ratio"] == 0.774
    assert isinstance(obj["ts"], (int, float))
    assert obj["stale"] is False


def test_ratio_json_stale_flag(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.5, session_id=SID, stale=True)  # 700 s old
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stale"] is True


def test_ratio_json_explicit_slug_from_elsewhere(cc_home, tmp_path,
                                                 monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.4, session_id=SID)
    elsewhere = make_repo(tmp_path, name="elsewhere-repo")
    monkeypatch.chdir(elsewhere)
    assert cli.main(["ratio", "--json", "spike-repo"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["slug"] == "spike-repo"
    assert obj["ratio"] == 0.4


def test_ratio_json_no_metrics_exits_1_plain_stderr(cc_home, tmp_path,
                                                    monkeypatch, capsys):
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # no JSON on failure
    assert "no metrics" in captured.err


def test_ratio_plain_output_unchanged_after_json_flag(cc_home, tmp_path,
                                                      monkeypatch, capsys):
    # Default output contract must not move for scripts that parse it.
    repo = make_repo(tmp_path)
    register_session(repo, session_id=SID)
    set_metric(0.774, session_id=SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ratio"]) == 0
    assert capsys.readouterr().out == "spike-repo 0.774\n"


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


def _unregistered_with_marker(tmp_path):
    """A repo with a status-line runtime marker but no record — the
    Phase 28 late-attach case (live conversation, never registered)."""
    from cc_continuity import project as _project
    from cc_continuity import statusline as _sl
    repo = make_repo(tmp_path)
    root = _project.resolve_root(repo)
    _sl.write_metric({"session_id": SID, "project_dir": str(root),
                      "ts": time.time()})
    return repo, root


def test_cli_checkpoint_unregistered_registers_and_finalizes(cc_home, tmp_path):
    # Phase 28: the running conversation has no record — the command
    # registers it (counter name, source=manual) and checkpoints it,
    # instead of erroring with "No registered session".
    from cc_continuity import paths as _paths
    from cc_continuity import project as _project
    repo, root = _unregistered_with_marker(tmp_path)
    r = _run_cli("checkpoint", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": ""})
    assert r.returncode == 0, r.stderr
    assert "registered 001" in r.stdout
    assert "DEGRADED" in r.stdout
    key = _project.project_key_for_root(root)
    st = state.load_state(key, 1)
    assert st["session_id"] == SID
    assert st["name"] == "spike-repo-001"
    assert st["state"] == "CHECKPOINT_VERIFIED"
    entries = ledger.read(_paths.session_dir(key, 1))
    entries[0].pop("ts")  # ledger.append always adds ts
    assert entries[0] == {"type": "session_start", "source": "manual",
                          "name": "spike-repo-001", "nnn": 1}


def test_cli_request_rotation_unregistered_registers(cc_home, tmp_path):
    # Phase 28: the same late-attach hole, same fix, for request-rotation.
    from cc_continuity import project as _project
    repo, root = _unregistered_with_marker(tmp_path)
    r = _run_cli("request-rotation", cwd=repo,
                 env_extra={"CC_CONTINUITY_SESSION": ""})
    assert r.returncode == 0, r.stderr
    assert "registered 001" in r.stdout
    assert "Rotation pending" in r.stdout
    key = _project.project_key_for_root(root)
    assert state.load_state(key, 1)["state"] == "ROTATION_PENDING"


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


# ------------------------------------------------------------- repair cmds


def _repair_fixture(tmp_path):
    """Connector-style junk ledger: 151 BOOTSTRAPPING (dead sid), 152/153
    ACTIVE ghosts (registered but never a conversation), 154 ACTIVE live
    (fresh metric). Returns (repo, key)."""
    repo = make_repo(tmp_path)
    register_session(repo)  # nnn 001, irrelevant background record
    for nnn, st in ((151, "BOOTSTRAPPING"), (152, "ACTIVE"),
                    (153, "ACTIVE"), (154, "ACTIVE")):
        state.create_session_state(key := _cli_project_key(repo),
                                   f"spike-repo-{nnn}", nnn, f"sid-{nnn}",
                                   initial_state=st)
    set_metric(0.3, session_id="sid-154")  # fresh metric: 154 is live
    return repo, key


def _cli_project_key(repo):
    from cc_continuity import project as _project
    return _project.project_key_for_root(_project.resolve_root(repo))


def test_repair_sessions_ghost_and_close(cc_home, tmp_path, capsys):
    """Ghost marks off never-conversed records; close terminalizes the dead
    BOOTSTRAPPING record; the live record is left untouched (user directive
    2026-11-03: repair must never break the live structure)."""
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", key,
                   "--ghost", "152,153", "--close", "151"])
    assert rc == 0, capsys.readouterr().err
    for nnn in (152, 153):
        rec = state.load_state(key, nnn)
        assert rec["off"] is True and rec["state"] == "ACTIVE"
        assert any("ghost-off" in json.dumps(e)
                   for e in ledger.read(paths.session_dir(key, nnn)))
    rec151 = state.load_state(key, 151)
    assert rec151["state"] == "ROTATED" and rec151["successor"] is None
    assert any("manual_close" in json.dumps(e)
               for e in ledger.read(paths.session_dir(key, 151)))
    rec154 = state.load_state(key, 154)
    assert rec154["state"] == "ACTIVE" and rec154["off"] is False


def test_repair_close_refuses_live_metric(cc_home, tmp_path, capsys):
    """A record with a fresh statusline metric is live — never closed."""
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", key, "--close", "154"])
    assert rc == 1
    assert "live" in capsys.readouterr().err.lower()
    assert state.load_state(key, 154)["state"] == "ACTIVE"


def test_repair_ghost_refuses_conversed(cc_home, tmp_path, capsys):
    """A record whose ledger holds a user_prompt had a conversation —
    ghost-off is refused (close is the admin path for those)."""
    repo, key = _repair_fixture(tmp_path)
    ledger.append(paths.session_dir(key, 152),
                  {"type": "user_prompt", "text": "real work"})
    rc = cli.main(["repair", "sessions", "--project", key, "--ghost", "152"])
    assert rc == 1
    assert "conversation" in capsys.readouterr().err.lower()
    assert state.load_state(key, 152)["off"] is False


def test_repair_ghost_refuses_live_metric(cc_home, tmp_path, capsys):
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", key, "--ghost", "154"])
    assert rc == 1
    assert "live" in capsys.readouterr().err.lower()
    assert state.load_state(key, 154)["off"] is False


def test_repair_close_idempotent_terminal(cc_home, tmp_path, capsys):
    """Closing an already-ROTATED record is a no-op success."""
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", key, "--close", "151"])
    assert rc == 0
    assert cli.main(["repair", "sessions", "--project", key,
                     "--close", "151"]) == 0
    assert state.load_state(key, 151)["state"] == "ROTATED"


def test_repair_unknown_project_exits_1(cc_home, tmp_path, capsys):
    repo = make_repo(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", "nope-00000000",
                   "--close", "1"])
    assert rc == 1
    assert "nope-00000000" in capsys.readouterr().err


def test_repair_unknown_record_exits_1(cc_home, tmp_path, capsys):
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", key, "--close", "777"])
    assert rc == 1
    assert state.StateNotFoundError


def test_repair_accepts_repo_path(cc_home, tmp_path, capsys):
    """--project also accepts a repo path, resolved like every other cmd."""
    repo, key = _repair_fixture(tmp_path)
    rc = cli.main(["repair", "sessions", "--project", str(repo),
                   "--close", "151"])
    assert rc == 0
    assert state.load_state(key, 151)["state"] == "ROTATED"


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
    # escalation loop (spec 2026-11-03) shrinks too — same pattern as
    # test_rotator._shrink_deadline: deadline covers attempt 1 + escalation.
    monkeypatch.setattr(rotator, "RESUME_DEADLINE_S", 1.0)
    monkeypatch.setattr(rotator, "RETRY_POLL_S", 0.05)
    monkeypatch.setattr(rotator, "RENAME_ACK_WAIT_S", 0.1)
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


def _show_lines(capsys):
    """Parse 'key value' lines of configure --show into a dict."""
    return dict(line.split(" ", 1)
                for line in capsys.readouterr().out.splitlines() if line)


def test_configure_show_defaults_only(cc_home, capsys):
    # No config file at all: the view is the pure defaults.
    assert cli.main(["configure", "--show"]) == 0
    lines = _show_lines(capsys)
    assert lines["warn_ratio"] == "0.55"
    assert lines["rotate_ratio"] == "0.62"
    assert lines["window_tokens"] == "262144"
    assert lines["progress_type"] == "0"
    assert lines["turn_clock"] == "false"


def test_configure_show_file_overlay(cc_home, capsys):
    # File values win; keys the file omits still show their defaults;
    # the installer stamp block never appears.
    (cc_home / "config.json").write_text(json.dumps(
        {"turn_clock": True, "progress_type": 1,
         "install": {"version": "9.9.9", "installed_at": "x",
                     "settings_backup": "/tmp/y"}}),
        encoding="utf-8")
    assert cli.main(["configure", "--show"]) == 0
    out = capsys.readouterr().out
    lines = dict(line.split(" ", 1)
                 for line in out.splitlines() if line)
    assert lines["turn_clock"] == "true"
    assert lines["progress_type"] == "1"
    assert lines["warn_ratio"] == "0.55"  # default still filled in
    assert "install" not in lines
    assert "installed_at" not in out


def test_configure_show_json(cc_home, capsys):
    (cc_home / "config.json").write_text(
        json.dumps({"unattended": True, "install": {"version": "9.9.9"}}),
        encoding="utf-8")
    assert cli.main(["configure", "--show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unattended"] is True
    assert data["hard_ratio"] == 0.72  # defaulted in
    assert "install" not in data
    assert list(data) == sorted(data)  # deterministic key order


def test_configure_show_missing_file_note(cc_home, capsys):
    # Missing config file is not an error: defaults print, rc 0, and a
    # one-line note goes to stderr (stdout stays pure data).
    assert cli.main(["configure", "--show"]) == 0
    cap = capsys.readouterr()
    assert "warn_ratio 0.55" in cap.out
    assert "no config file" in cap.err.lower()
    assert not (cc_home / "config.json").exists()


def test_configure_show_never_writes(cc_home):
    cfg_path = cc_home / "config.json"
    cfg_path.write_text(json.dumps({"turn_clock": True}), encoding="utf-8")
    before_bytes = cfg_path.read_bytes()
    before_ns = cfg_path.stat().st_mtime_ns
    assert cli.main(["configure", "--show"]) == 0
    assert cfg_path.read_bytes() == before_bytes
    assert cfg_path.stat().st_mtime_ns == before_ns
    assert cli.main(["configure", "--show", "--json"]) == 0
    assert cfg_path.read_bytes() == before_bytes
    assert cfg_path.stat().st_mtime_ns == before_ns


def test_configure_show_conflict_with_change_flags(cc_home, capsys):
    assert cli.main(["configure", "--show", "--ntfy",
                     "https://ntfy.sh/t"]) == 1
    assert "--show" in capsys.readouterr().err
    assert not (cc_home / "config.json").exists()


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
    assert len(sent) == 1
    title, body = sent[0]
    assert title == "✅ cc-continuity: task complete"
    # issue #7 G1: envelope lines prepend the template line; host/branch/
    # model lines are machine-dependent (B3 omits what's unavailable), so
    # pin only the deterministic envelope row + the template suffix.
    assert body.splitlines()[0] == "proj=spike-repo 001 (spike-repo-001)"
    assert body.endswith("\nproj=spike-repo summary=all 14 tasks green")
    last = ledger.read(paths.session_dir(key, nnn))[-1]
    last.pop("ts")  # ledger.append always adds ts
    assert last == {"type": "task_done", "summary": "all 14 tasks green"}


def test_signal_dry_run_renders_without_sending(cc_home, tmp_path, monkeypatch,
                                                capsys):
    # issue #30: --dry-run renders the exact push and sends nothing
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")
    key, nnn = register_session(repo)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda *a, **kw: sent.append(a) or True)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "--dry-run", "done", "all tests green"]) == 0
    assert sent == []  # nothing leaves the machine
    out = capsys.readouterr().out
    assert "title: ✅ cc-continuity: task complete" in out
    assert "priority: default" in out
    assert "summary=all tests green" in out
    types = [e["type"] for e in ledger.read(paths.session_dir(key, nnn))]
    assert "task_done" not in types  # ledger untouched too


def test_signal_dry_run_reports_disabled(cc_home, tmp_path, monkeypatch,
                                         capsys):
    # disabled notify must be visible in the render instead of a silent no-op
    repo = make_repo(tmp_path)
    register_session(repo)  # default cc_home config: notify disabled
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "--dry-run", "needs-human", "stuck?"]) == 0
    out = capsys.readouterr().out
    assert "would NOT send" in out
    assert "🛑 cc-continuity: BLOCKED" in out


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


# ------------------- Phase 12: progress-pane subcommand (mode 1)


def test_progress_pane_subparser_smoke():
    ns = cli.build_parser().parse_args(["progress-pane"])
    assert ns.command == "progress-pane"


def test_cli_progress_pane_runs_animator(cc_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cc_continuity.progress_pane.run_animator",
        lambda **kw: calls.append(kw))
    assert cli.main(["progress-pane"]) == 0
    # focus dwell v2 (2026-11-07): the real iTerm2 frontmost probe is wired
    assert calls[0]["window_id"] is None  # no arg, no $TMUX_PANE
    assert (calls[0]["_frontmost_tty"]
            is cli.blink_watch._default_frontmost_tty)


def test_cli_progress_pane_passes_window_id(cc_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cc_continuity.progress_pane.run_animator",
        lambda **kw: calls.append(kw))
    assert cli.main(["progress-pane", "@7"]) == 0
    assert calls[0]["window_id"] == "@7"
    assert (calls[0]["_frontmost_tty"]
            is cli.blink_watch._default_frontmost_tty)


# --------------- blink-watch subcommand (mode-0 poller, live bug 11-07)


def test_blink_watch_subparser_smoke():
    ns = cli.build_parser().parse_args(
        ["blink-watch", "--pane", "%7", "--window", "@7",
         "--session-dir", "/s"])
    assert ns.command == "blink-watch"
    assert (ns.pane, ns.window, ns.session_dir) == ("%7", "@7", "/s")


def test_cli_blink_watch_runs_watch(cc_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cc_continuity.blink_watch.watch",
        lambda sd, **kw: calls.append((sd, kw)))
    assert cli.main(["blink-watch", "--pane", "%7", "--window", "@7",
                     "--session-dir", "/s"]) == 0
    sd, kw = calls[0]
    assert sd == "/s"
    assert (kw["pane_id"], kw["window_id"]) == ("%7", "@7")
    assert isinstance(kw["cfg"], dict)


# ------------------------------------------------------- notify CLI (issue #7)


class _NotifySub:
    def __init__(self):
        self.popen_calls = []
        self.run_calls = []

    def Popen(self, cmd, **kw):
        self.popen_calls.append((cmd, kw))
        return object()

    def run(self, cmd, **kw):
        self.run_calls.append((cmd, kw))
        return type("P", (), {"returncode": 0})()


def _notify_cfg(cc_home):
    (cc_home / "config.json").write_text(json.dumps(
        {"notify_enabled": True,
         "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")


def test_cli_notify_stop_spawns(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    _notify_cfg(cc_home)
    register_session(repo)
    monkeypatch.chdir(repo)
    payload = {"session_id": SID,
               "transcript_path": str(repo / "t.jsonl"),
               "cwd": str(repo)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    sub = _NotifySub()
    monkeypatch.setattr(notify, "subprocess", sub)
    assert cli.main(["notify", "stop"]) == 0
    assert len(sub.popen_calls) == 1
    cmd = sub.popen_calls[0][0]
    assert cmd[3] == "notify-emit"
    spec = json.loads(open(cmd[4], encoding="utf-8").read())
    assert spec["event"] == "stop_done"
    assert spec["source"] == "cli"
    assert spec["sid"] == SID
    assert spec["fields"]["transcript_path"] == str(repo / "t.jsonl")


def test_cli_notify_notification_dedup_minute_bucket(cc_home, tmp_path,
                                                     monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    _notify_cfg(cc_home)
    register_session(repo)
    monkeypatch.chdir(repo)
    payload = {"session_id": SID, "message": "deploy? y/n", "cwd": str(repo)}
    sub = _NotifySub()
    monkeypatch.setattr(notify, "subprocess", sub)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["notify", "notification"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["notify", "notification"]) == 0  # same minute -> dedup
    assert len(sub.popen_calls) == 1
    cmd = sub.popen_calls[0][0]
    spec = json.loads(open(cmd[4], encoding="utf-8").read())
    assert spec["event"] == "notification"
    assert spec["fields"]["msg"] == "deploy? y/n"


def test_cli_notify_emit_corrupt_exits_zero(cc_home, tmp_path):
    _notify_cfg(cc_home)
    bad = tmp_path / "spec.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["notify-emit", str(bad)]) == 0
    assert not bad.exists()  # corrupt spec consumed, not left in queue


def test_notify_claimed_passes_prev_and_handoff(cc_home, tmp_path,
                                                monkeypatch):
    from cc_continuity import notify
    from cc_continuity.hooks import common
    captured = {}
    def fake(cfg, event, **kw):
        captured["event"] = event
        captured.update(kw)
        return True
    monkeypatch.setattr(notify, "safe_send", fake)
    res = {"new_nnn": "101", "new_name": "proj-101",
           "old_name": "proj-99", "old_nnn": "99",
           "validation": "OK", "sid": SID, "key": "k1"}
    common.notify_claimed(_ := {}, "k1", res)
    assert captured["prev"] == "proj-99"
    assert captured["handoff_path"].endswith(
        "sessions/099/checkpoint/handoff.md")


# ------------------------------------------------------- completion (#50)


def _registered_subcommands():
    """Top-level subcommand names from the live argparse tree."""
    sub = next(a for a in cli.build_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    return list(sub.choices)


def test_completion_zsh_lists_every_subcommand(capsys):
    # The zsh script must offer every subcommand the CLI really registers.
    assert cli.main(["completion", "zsh"]) == 0
    out = capsys.readouterr().out
    assert "#compdef cc-continuity" in out
    for name in _registered_subcommands():
        assert name in out


def test_completion_bash_lists_every_subcommand(capsys):
    assert cli.main(["completion", "bash"]) == 0
    out = capsys.readouterr().out
    assert "complete -F" in out
    for name in _registered_subcommands():
        assert name in out


def test_completion_unknown_shell_exits_1(capsys):
    # Clear stderr line naming the bad shell and the supported ones; no
    # partial script on stdout; rc 1 (not argparse's rc 2).
    assert cli.main(["completion", "fish"]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "fish" in cap.err
    assert "bash" in cap.err and "zsh" in cap.err


def test_completion_scripts_are_deterministic(capsys):
    # Same tree -> byte-identical script every run (no set/dict iteration
    # order leaking into the output).
    assert cli.main(["completion", "bash"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["completion", "bash"]) == 0
    assert capsys.readouterr().out == first
    assert cli.main(["completion", "zsh"]) == 0
    zfirst = capsys.readouterr().out
    assert cli.main(["completion", "zsh"]) == 0
    assert capsys.readouterr().out == zfirst


def test_completion_scripts_match_parse_args(capsys):
    # Generated from the same argparse tree the CLI builds: the level-1
    # token list in the bash script must equal the parser's real commands,
    # and each must parse without "invalid choice" (a rename in the tree
    # that a hardcoded script missed fails here).
    import re
    assert cli.main(["completion", "bash"]) == 0
    out = capsys.readouterr().out
    tokens = re.findall(r'compgen -W "([^"]*)"', out)[0].split()
    assert set(tokens) == set(_registered_subcommands())
    parser = cli.build_parser()
    for token in tokens:
        try:
            parser.parse_args([token])
        except SystemExit:
            err = capsys.readouterr().err
            assert "invalid choice" not in err
