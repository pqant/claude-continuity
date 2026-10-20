"""launch.py: plan_launch (pure) + run_launch (fake tmux/claude)."""

import json
import os

import pytest

from cc_continuity import launch, paths, project

REPO = "/work/myproj"


@pytest.fixture
def fake_cc_home_repo(tmp_path, monkeypatch, cc_home):
    """Project counter already at 3 for REPO (name-prediction test).

    REPO need not exist: ``resolve_root`` returns the cwd itself for
    non-git dirs, so the key is computed from the literal path.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    key = project.project_key_for_root(REPO)
    pdir = paths.project_dir(key)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text(json.dumps(
        {"slug": "myproj", "root": REPO, "counter": 3,
         "latest_checkpoint": None, "sessions": [], "created": "now"}),
        encoding="utf-8")


def _plan(argv, **kw):
    base = dict(cwd=REPO, in_tmux=False, interactive=True)
    base.update(kw)
    return launch.plan_launch(argv, **base)


def test_disabled_passthrough():
    p = _plan(["-n", "x"], disabled=True)
    assert p.mode == "passthrough" and p.claude_args == ("-n", "x")
    assert p.name is None and p.tmux_session is None


def test_no_tmux_env_passthrough():
    p = _plan([], no_tmux=True)
    assert p.mode == "passthrough"


def test_non_interactive_outside_tmux_detached_mode():
    # Non-TTY launch (background job/script): the session still gets a tmux
    # PANE so $TMUX_PANE-based rotation + the animated progress pane work —
    # a detached session, never a pane-less process.
    p = _plan([], interactive=False, rand=lambda: "a1b2")
    assert p.mode == "detached"
    assert p.name == "myproj-001"
    assert p.claude_args == ("-n", "myproj-001")
    assert p.tmux_session == "cc-myproj-a1b2"


def test_non_interactive_inside_tmux_direct_mode():
    p = _plan([], interactive=False, in_tmux=True)
    assert p.mode == "direct"
    assert p.name == "myproj-001"
    assert p.tmux_session is None


@pytest.mark.parametrize("argv", (["-p", "hi"], ["--version"], ["mcp", "list"]))
def test_non_interactive_pass_stays_passthrough(argv):
    # Flags/subcommands never get a tmux session, TTY or not.
    assert _plan(argv, interactive=False).mode == "passthrough"


def test_non_interactive_resume_flag_detached_no_n():
    p = _plan(["-c"], interactive=False, rand=lambda: "a1b2")
    assert p.mode == "detached"
    assert p.name is None
    assert p.claude_args == ("-c",)
    assert p.tmux_session == "cc-myproj-a1b2"


@pytest.mark.parametrize("flag", ["-p", "--print", "--version", "-v",
                                   "--help", "-h", "--bg", "--background",
                                   "--bare"])
def test_pass_through_flags(flag):
    assert _plan([flag]).mode == "passthrough"


@pytest.mark.parametrize("sub", sorted(launch.PASS_SUBCOMMANDS))
def test_pass_through_subcommands(sub):
    p = _plan([sub, "list"])
    assert p.mode == "passthrough"
    assert p.claude_args == (sub, "list")


def test_outside_tmux_fresh_project_tmx_mode_and_name():
    p = _plan([], rand=lambda: "a1b2")
    assert p.mode == "tmux"
    assert p.name == "myproj-001"
    assert p.claude_args == ("-n", "myproj-001")
    assert p.tmux_session == "cc-myproj-a1b2"


def test_inside_tmux_direct_mode():
    p = _plan([], in_tmux=True)
    assert p.mode == "direct"
    assert p.name == "myproj-001"
    assert p.tmux_session is None


def test_next_counter_follows_project(fake_cc_home_repo):
    p = _plan([])
    assert p.name == "myproj-004"  # counter already at 3


def test_user_name_flag_suppresses_n():
    for argv in (["-n", "foo"], ["--name", "foo"], ["--name=foo"]):
        p = _plan(argv)
        assert p.name is None
        # no ADDITIONAL -n appended: args pass through unchanged
        # (argv itself may contain the user's own -n)
        assert p.claude_args == tuple(argv)
        assert p.mode == "tmux"


@pytest.mark.parametrize("argv", (["-r"], ["-r", "abc"], ["--resume", "abc"],
                                  ["-c"], ["--continue"]))
def test_resume_continue_flags_suppress_n(argv):
    p = _plan(argv)
    assert p.name is None
    assert p.mode == "tmux"  # still tmux-wrapped, just no -n


def test_prompt_arguments_do_not_block():
    p = _plan(["fix", "the", "bug"])
    assert p.mode == "tmux"
    assert p.claude_args == ("fix", "the", "bug", "-n", "myproj-001")


def test_value_flags_do_not_block():
    assert _plan(["--model", "qwen3.8-27b"]).mode == "tmux"
    assert _plan(["--effort", "low", "hi"]).mode == "tmux"


def test_name_appended_after_original_args():
    p = _plan(["--model", "qwen3.8-27b"])
    assert p.claude_args == ("--model", "qwen3.8-27b", "-n", "myproj-001")


# ------------------------------------------------------------ run_launch

import shutil
import subprocess
import sys
import types
from pathlib import Path

FAKE_CLAUDE = """#!/bin/sh
{
  printf 'argv:'
  for a in "$@"; do printf ' %s' "$a"; done
  printf '\\nCC_CONTINUITY_NAME=%s\\nTMUX=%s\\nANTHROPIC_MODEL=%s\\n' \\
    "$CC_CONTINUITY_NAME" "$TMUX" "$ANTHROPIC_MODEL"
} > "${FAKE_CLAUDE_OUT:?}"
exit 0
"""

FAKE_TMUX = """#!/bin/sh
printf '%s\\n' "---" "$@" >> "${FAKE_TMUX_LOG:?}"
exit 0
"""


@pytest.fixture
def work_env(tmp_path, monkeypatch, cc_home):
    """Temp HOME, a git repo cwd, fake claude/tmux, forced interactive."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "myproj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(FAKE_CLAUDE); os.chmod(fake_claude, 0o755)
    fake_tmux = tmp_path / "fake-tmux"
    fake_tmux.write_text(FAKE_TMUX); os.chmod(fake_tmux, 0o755)
    out = tmp_path / "claude.out"
    log = tmp_path / "tmux.log"
    env = dict(os.environ)
    env.update({
        "CC_CONTINUITY_CLAUDE_BIN": str(fake_claude),
        "CC_CONTINUITY_TMUX_BIN": str(fake_tmux),
        "FAKE_CLAUDE_OUT": str(out),
        "FAKE_TMUX_LOG": str(log),
        "CC_CONTINUITY_FORCE_INTERACTIVE": "1",
        "ANTHROPIC_MODEL": "qwen3.8-27b",
    })
    env.pop("TMUX", None)
    env.pop("CC_CONTINUITY_NAME", None)
    env.pop("CC_CONTINUITY_NO_TMUX", None)
    return types.SimpleNamespace(home=home, repo=repo, out=out, log=log,
                                 claude=fake_claude, env=env)


def _run_launch(env, *args):
    e = dict(env.env)
    e["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-m", "cc_continuity", "launch", *args],
        cwd=env.repo, env=e, capture_output=True, text=True, timeout=60)


def test_exec_passthrough_print_mode(work_env):
    r = _run_launch(work_env, "-p", "hello")
    assert r.returncode == 0
    assert not work_env.log.exists()          # no tmux call
    text = work_env.out.read_text(encoding="utf-8")
    assert text.startswith("argv: -p hello")
    assert "CC_CONTINUITY_NAME=" in text and text.splitlines()[1] == "CC_CONTINUITY_NAME="


def test_exec_passthrough_subcommand(work_env):
    r = _run_launch(work_env, "mcp", "list")
    assert r.returncode == 0 and not work_env.log.exists()
    assert work_env.out.read_text(encoding="utf-8").startswith("argv: mcp list")


def test_exec_tmux_mode_creates_and_attaches(work_env):
    r = _run_launch(work_env)
    assert r.returncode == 0
    calls = []
    for line in work_env.log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            calls.append([])
        elif calls:
            calls[-1].append(line)
    assert calls[0][0:3] == ["new-session", "-d", "-s"]
    assert calls[0][3].startswith("cc-myproj-") and len(calls[0][3]) == len("cc-myproj-") + 4
    assert calls[0][4:6] == ["-c", str(work_env.repo)]
    cmd = calls[0][6]
    assert "CC_CONTINUITY_NAME=myproj-001" in cmd
    assert "ANTHROPIC_MODEL=qwen3.8-27b" in cmd
    # `claude_bin` is the FAKE claude path (CC_CONTINUITY_CLAUDE_BIN), not a
    # literal "claude"
    assert f"command {work_env.claude}" in cmd
    assert cmd.rstrip().endswith("-n myproj-001")
    assert calls[1][:2] == ["attach-session", "-t"]
    assert calls[1][2] == calls[0][3]


def test_exec_detached_mode_creates_without_attach(work_env):
    e = dict(work_env.env)
    e.pop("CC_CONTINUITY_FORCE_INTERACTIVE", None)  # child is non-TTY
    env2 = types.SimpleNamespace(**{**vars(work_env), "env": e})
    r = _run_launch(env2)
    assert r.returncode == 0
    calls = []
    for line in work_env.log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            calls.append([])
        elif calls:
            calls[-1].append(line)
    assert calls[0][0:3] == ["new-session", "-d", "-s"]
    assert calls[0][3].startswith("cc-myproj-") and len(calls[0][3]) == len("cc-myproj-") + 4
    assert calls[0][4:6] == ["-c", str(work_env.repo)]
    cmd = calls[0][6]
    assert f"command {work_env.claude}" in cmd
    assert cmd.rstrip().endswith("-n myproj-001")
    assert len(calls) == 1          # NO attach-session call
    assert work_env.out.exists() is False  # claude not exec'd by launcher
    assert "tmux attach" in r.stderr      # attach hint for the user


def test_exec_detached_unattended_flag_in_pane_cmd(work_env):
    e = dict(work_env.env)
    e.pop("CC_CONTINUITY_FORCE_INTERACTIVE", None)
    env2 = types.SimpleNamespace(**{**vars(work_env), "env": e})
    r = _run_launch(env2, "--unattended")
    assert r.returncode == 0
    assert "--dangerously-skip-permissions" in \
        work_env.log.read_text(encoding="utf-8")


def test_exec_direct_mode_inside_tmux(work_env):
    e = dict(work_env.env); e["TMUX"] = "/tmp/tmux-501/default,123,0"
    env2 = types.SimpleNamespace(**{**vars(work_env), "env": e})
    r = _run_launch(env2)
    assert r.returncode == 0 and not work_env.log.exists()
    text = work_env.out.read_text(encoding="utf-8")
    assert text.startswith("argv: -n myproj-001")
    assert text.splitlines()[1] == "CC_CONTINUITY_NAME=myproj-001"
    assert "TMUX=/tmp/tmux-501/default,123,0" in text


def test_exec_tmux_missing_falls_back_to_direct(work_env):
    e = dict(work_env.env); e["CC_CONTINUITY_TMUX_BIN"] = "/nonexistent/tmux"
    env2 = types.SimpleNamespace(**{**vars(work_env), "env": e})
    r = _run_launch(env2)
    assert r.returncode == 0
    assert "tmux not found" in r.stderr
    assert work_env.out.read_text(encoding="utf-8").startswith("argv: -n myproj-001")


def test_exec_user_name_flag_not_overridden(work_env):
    r = _run_launch(work_env, "--name", "special")
    assert r.returncode == 0
    calls = []
    for line in work_env.log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            calls.append([])
        elif calls:
            calls[-1].append(line)
    cmd = calls[0][-1]  # the command string new-session runs
    assert "--name special" in cmd
    assert "CC_CONTINUITY_NAME=" not in cmd


def test_exec_disabled_passthrough(work_env):
    e = dict(work_env.env); e["CC_CONTINUITY"] = "0"
    env2 = types.SimpleNamespace(**{**vars(work_env), "env": e})
    r = _run_launch(env2)
    assert r.returncode == 0 and not work_env.log.exists()
    assert work_env.out.read_text(encoding="utf-8") == \
        "argv:\nCC_CONTINUITY_NAME=\nTMUX=\nANTHROPIC_MODEL=qwen3.8-27b\n"
    # no "-n" appended, no tmux
    assert "-n" not in work_env.out.read_text(encoding="utf-8")


# ------------------------------------------------- Phase 9: --unattended


def test_unattended_flag_tmux_mode(work_env):
    r = _run_launch(work_env, "--unattended")
    assert r.returncode == 0
    log = work_env.log.read_text(encoding="utf-8")
    assert "--dangerously-skip-permissions" in log
    assert work_env.out.exists() is False  # tmux mode: claude not exec'd here


def test_unattended_flag_direct_mode(work_env):
    e = dict(work_env.env, TMUX="/tmp/fake-sock,42,0")
    e["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    r = subprocess.run(
        [sys.executable, "-m", "cc_continuity", "launch", "--unattended"],
        cwd=work_env.repo, env=e, capture_output=True, text=True,
        timeout=60)
    assert r.returncode == 0
    assert "--dangerously-skip-permissions" in \
        work_env.out.read_text(encoding="utf-8")


def test_unattended_config_inherited_without_flag(work_env, cc_home):
    (cc_home / "config.json").write_text(
        json.dumps({"unattended": True}), encoding="utf-8")
    r = _run_launch(work_env)
    assert r.returncode == 0
    assert "--dangerously-skip-permissions" in \
        work_env.log.read_text(encoding="utf-8")


def test_unattended_passthrough_untouched(work_env):
    r = _run_launch(work_env, "--unattended", "-p", "hello")
    assert r.returncode == 0
    text = work_env.out.read_text(encoding="utf-8")
    assert text.startswith("argv: -p hello")
    assert "--dangerously-skip-permissions" not in text
    assert not work_env.log.exists()


def test_default_launch_not_unattended(work_env):
    r = _run_launch(work_env)
    assert r.returncode == 0
    assert "--dangerously-skip-permissions" not in \
        work_env.log.read_text(encoding="utf-8")
