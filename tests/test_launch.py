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


def test_non_interactive_passthrough():
    assert _plan([], interactive=False).mode == "passthrough"


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
