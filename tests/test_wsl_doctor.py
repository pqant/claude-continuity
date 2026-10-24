"""doctor.py non-macOS branch (Task 7): shell, rc-block, wsl, daemon,
autostart checks.

The macOS branch is pinned byte-for-byte by tests/test_doctor.py; these
tests run with sys.platform monkeypatched to "linux" (no /proc on the
dev host, so hosting.kind() falls through to "linux" and select_host
returns TmuxHost — the WSL1 shape).
"""

import json
import sys

import pytest

from cc_continuity import daemon_supervisor, doctor, hosting, install, managed
from cc_continuity.install import ZSHRC_BEGIN, ZSHRC_END

from test_install import FakeRun


@pytest.fixture
def wsl_home(tmp_path, monkeypatch):
    """WSL1 home: manifest install manifest has rc_file=~/.bashrc
    (block written via hosting.rc_block_content([])) and platform=wsl1;
    CC_CONTINUITY_HOME points at the fixture store."""
    monkeypatch.setattr(sys, "platform", "linux")
    home = tmp_path / "home"
    home.mkdir()
    store = home / ".claude-continuity"
    store.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(store))
    rc_file = home / ".bashrc"
    rc_file.write_text(managed.upsert_block(
        "", ZSHRC_BEGIN, ZSHRC_END, hosting.rc_block_content([])),
        encoding="utf-8")
    (store / "config.json").write_text(json.dumps({
        "window_tokens": 262144,
        "install": {"rc_file": str(rc_file), "platform": "wsl1"}}),
        encoding="utf-8")
    return home


class WslRun:
    """FakeRun that also plays the Task 7 probes: tmux has-session (per
    live-session set) and schtasks.exe /query (fixed rc)."""

    def __init__(self, live=(), schtasks_rc=0):
        self.live = set(live)
        self.schtasks_rc = schtasks_rc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)

        class R:
            pass

        r = R()
        r.stdout = "2.1.209 (Claude Code)"
        r.stderr = ""
        r.returncode = 0
        if isinstance(cmd, list) and cmd:
            if cmd[0] == "tmux" and "has-session" in cmd:
                r.returncode = 0 if cmd[-1] in self.live else 1
            elif cmd[0] == "schtasks.exe":
                r.returncode = self.schtasks_rc
        return r


def _which_wsl(with_schtasks=True):
    def which(name):
        if name == "claude":
            return "/usr/bin/claude"
        if name in ("tmux", "bash", "zsh", "curl"):
            return "/usr/bin/" + name
        if name == "schtasks.exe":
            return "/usr/bin/schtasks.exe" if with_schtasks else None
        return None

    return which


# --- shell / rc-block (zsh / zshrc replacements) --------------------------


def test_doctor_shell_check_names_login_shell(wsl_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl())
    by = {c.name: c for c in checks}
    assert by["shell"].ok is True
    assert "bash" in by["shell"].detail
    assert "zsh" not in by  # replaced, not added


def test_doctor_rc_block_present_ok(wsl_home):
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl())
    by = {c.name: c for c in checks}
    assert by["rc-block"].ok is True
    assert ".bashrc" in by["rc-block"].detail
    assert "zshrc" not in by  # replaced, not added


def test_doctor_rc_block_missing_warns(wsl_home):
    # healthy non-macOS home but the managed block removed from ~/.bashrc
    (wsl_home / ".bashrc").write_text("export PATH=$PATH:/x\n",
                                      encoding="utf-8")
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl())
    rc = next(c for c in checks if c.name == "rc-block")
    assert rc.ok is False
    assert "re-run: cc-continuity install" in rc.detail


# --- wsl informational ------------------------------------------------------


def test_doctor_wsl1_informational(wsl_home):
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl())
    w = next(c for c in checks if c.name == "wsl")
    assert w.ok is True and w.warning is False
    assert "tmux host" in w.detail


# --- daemon -----------------------------------------------------------------


def _install_daemon_state(home, name="watchdog"):
    sp = daemon_supervisor.state_path(name, home)
    daemon_supervisor._save_state(sp, installed=True,
                                  session=f"cc-d-{name}")


def test_doctor_daemon_none_installed(wsl_home):
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl())
    d = next(c for c in checks if c.name == "daemon")
    assert d.ok is True and d.warning is False
    assert "none installed" in d.detail


def test_doctor_daemon_running_ok(wsl_home):
    _install_daemon_state(wsl_home)
    checks = doctor.run_doctor(wsl_home,
                               runner=WslRun(live={"cc-d-watchdog"}),
                               which=_which_wsl())
    d = next(c for c in checks if c.name == "daemon")
    assert d.ok is True and d.warning is False
    assert "running" in d.detail and "tmux" in d.detail


def test_doctor_daemon_not_running_warns(wsl_home):
    # TmuxHost installed (state file) but no tmux session
    _install_daemon_state(wsl_home)
    checks = doctor.run_doctor(wsl_home, runner=WslRun(live=set()),
                               which=_which_wsl())
    d = next(c for c in checks if c.name == "daemon")
    assert d.ok is True and d.warning is True
    assert "daemon ensure" in d.detail


# --- autostart (spec §7.5) ---------------------------------------------------


def test_doctor_autostart_present_ok(wsl_home):
    checks = doctor.run_doctor(wsl_home,
                               runner=WslRun(schtasks_rc=0),
                               which=_which_wsl(with_schtasks=True))
    a = next(c for c in checks if c.name == "autostart")
    assert a.ok is True and a.warning is False


def test_doctor_autostart_missing_warns(wsl_home):
    # schtasks query returns exit 1
    checks = doctor.run_doctor(wsl_home,
                               runner=WslRun(schtasks_rc=1),
                               which=_which_wsl(with_schtasks=True))
    a = next(c for c in checks if c.name == "autostart")
    assert a.warning is True
    assert "schtasks /Create" in a.detail


def test_doctor_autostart_interop_off_not_verifiable(wsl_home):
    # schtasks.exe not found (interop disabled) — skip with a note
    checks = doctor.run_doctor(wsl_home, runner=WslRun(),
                               which=_which_wsl(with_schtasks=False))
    a = next(c for c in checks if c.name == "autostart")
    assert a.ok is True and a.warning is False
    assert "not verifiable" in a.detail


# --- macOS regression pin ----------------------------------------------------


def test_doctor_macos_output_unchanged(tmp_path, monkeypatch):
    """On darwin the check-name list is the pre-Task-7 one: zsh, zshrc
    and the plist watchdog check stay; no shell/rc-block/wsl/daemon/
    autostart names appear."""
    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    install.install(home, window=262144, runner=FakeRun())
    which_ok = lambda n: "/usr/bin/" + n  # noqa: E731
    checks = doctor.run_doctor(home, runner=FakeRun(), which=which_ok)
    names = {c.name for c in checks}
    assert {"zsh", "zshrc", "watchdog"} <= names
    assert not (names & {"shell", "rc-block", "wsl", "daemon", "autostart"})
