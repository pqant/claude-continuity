# tests/test_integration_phase7.py
"""Full install → doctor → launch → uninstall cycle through the REAL CLI
subprocess (the same shape as Gate 7), in a fake HOME."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# plan's "/bin/true" does not exist on macOS — resolve portably
TRUE = shutil.which("true") or "/usr/bin/true"


@pytest.fixture
def fake_home(tmp_path, monkeypatch, cc_home):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


def _cli(env, *args, cwd=None):
    e = dict(env)
    e["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, "-m", "cc_continuity", *args],
                          capture_output=True, text=True, timeout=120,
                          env=e, cwd=cwd)


def test_full_cycle_via_cli(fake_home):
    home = fake_home
    base = dict(os.environ)
    env = {**base, "HOME": str(home),
           "CC_CONTINUITY_HOME": str(home / ".claude-continuity")}
    # settings seed (realistic)
    (home / ".claude").mkdir()
    seed = {"statusLine": {"type": "command",
                           "command": "~/.claude/bin/claude-hud-launch.sh"},
            "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144"}}
    before = json.dumps(seed, indent=2, ensure_ascii=False) + "\n"
    (home / ".claude" / "settings.json").write_text(before, encoding="utf-8")

    r = _cli(env, "install", "--window", "262144")
    assert r.returncode == 0, r.stderr
    assert "doctor" in r.stdout.lower() or "usage" in r.stdout.lower()

    r = _cli(env, "doctor", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert all(c["ok"] for c in data)

    r = _cli(env, "uninstall")
    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "settings.json").read_text(
        encoding="utf-8") == before  # byte-for-byte

    r = _cli(env, "doctor")
    assert r.returncode != 0  # uninstalled -> reds

    # sanity: the old "not implemented (Phase 7)" message is gone. Point the
    # fake claude at the system `true` so the passthrough execs something
    # harmless.
    env2 = {**env, "CC_CONTINUITY_CLAUDE_BIN": TRUE,
            "CC_CONTINUITY_FORCE_INTERACTIVE": "1"}
    r = _cli(env2, "launch", "-p", "x")
    assert r.returncode == 0
    assert "not implemented" not in r.stderr


def test_install_cli_reports_missing_prereq(fake_home, monkeypatch):
    # claude not on PATH -> exit 1 + clear message, nothing written
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity"),
           "PATH": "/nonexistent"}
    r = _cli(env, "install")
    assert r.returncode == 1
    assert "prerequisite" in r.stderr.lower()
    assert not (fake_home / ".claude").exists()


def test_launch_cli_rejects_no_such_subcommand_placeholder_gone(fake_home):
    for name in ("install", "uninstall", "doctor", "launch"):
        env = {**os.environ, "HOME": str(fake_home),
               "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity"),
               "CC_CONTINUITY_CLAUDE_BIN": TRUE,
               "CC_CONTINUITY_FORCE_INTERACTIVE": "1"}
        r = _cli(env, name, "--help" if name != "launch" else "-p",
                 *([] if name == "launch" else []))
        assert "not implemented" not in r.stderr


def test_install_cli_window_flag_lands_in_config(fake_home):
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity")}
    r = _cli(env, "install", "--window", "40000")
    assert r.returncode == 0, r.stderr
    cfg = json.loads((fake_home / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["window_tokens"] == 40000


def test_doctor_cli_human_output_has_symbols(fake_home):
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity")}
    assert _cli(env, "install", "--window", "262144").returncode == 0
    r = _cli(env, "doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ python" in r.stdout
    assert "✗" not in r.stdout


def test_uninstall_cli_purge_flag(fake_home):
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity")}
    assert _cli(env, "install", "--window", "262144").returncode == 0
    r = _cli(env, "uninstall", "--purge")
    assert r.returncode == 0, r.stderr
    assert not (fake_home / ".claude-continuity").exists()


def test_install_cli_twice_second_still_ok(fake_home):
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity")}
    assert _cli(env, "install", "--window", "262144").returncode == 0
    r2 = _cli(env, "install", "--window", "262144")
    assert r2.returncode == 0, r2.stderr
    s = json.loads((fake_home / ".claude" / "settings.json")
                   .read_text(encoding="utf-8"))
    for entries in s["hooks"].values():
        assert len([e for e in entries if "cc-continuity" in json.dumps(e)]) \
            == 1


def test_usage_summary_printed_after_install(fake_home):
    env = {**os.environ, "HOME": str(fake_home),
           "CC_CONTINUITY_HOME": str(fake_home / ".claude-continuity")}
    r = _cli(env, "install", "--window", "262144")
    assert r.returncode == 0, r.stderr
    assert "/continuity-status" in r.stdout
    assert "cc-continuity uninstall" in r.stdout
