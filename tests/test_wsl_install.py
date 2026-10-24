"""install.py non-macOS branch (Task 6): rc_target, WSL prerequisites,
platform-gated install step 4 + uninstall rc cleanup.

The macOS branch is pinned byte-for-byte by tests/test_install.py —
these tests run with sys.platform monkeypatched to "linux".
"""

import json

import pytest

from cc_continuity import install

from test_install import FakeRun


@pytest.fixture
def fake_home(tmp_path, monkeypatch, cc_home):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


# --- rc_target -------------------------------------------------------------


def test_rc_target_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert str(install.rc_target(tmp_path)).endswith(".bashrc")


def test_rc_target_zsh(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert str(install.rc_target(tmp_path)).endswith(".zshrc")


def test_rc_target_fish_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    with pytest.raises(install.InstallError, match="not supported"):
        install.rc_target(tmp_path)


# --- prerequisites ---------------------------------------------------------


def test_prereq_bash_suffices_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr("shutil.which",
                        lambda n: "/bin/bash" if n in ("bash", "tmux",
                                                       "curl", "claude")
                        else None)
    missing = install.check_prerequisites(
        tmp_path,
        runner=lambda *a, **k: type("R", (), {
            "stdout": "2.1.209 (Claude Code)", "returncode": 0})())
    assert missing == []


def test_prereq_shell_missing_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr("shutil.which",
                        lambda n: "/bin/tmux" if n in ("tmux", "curl",
                                                       "claude")
                        else None)
    missing = install.check_prerequisites(
        tmp_path,
        runner=lambda *a, **k: type("R", (), {
            "stdout": "2.1.209 (Claude Code)", "returncode": 0})())
    assert missing == ["zsh or bash on PATH"]


# --- install step 4 (non-macOS) --------------------------------------------


def _manifest(home):
    return json.loads((home / ".claude-continuity" / "config.json")
                      .read_text(encoding="utf-8"))["install"]


def test_rc_block_written_to_symlink_target(fake_home, monkeypatch):
    # .bashrc is a symlink elsewhere; the block must land in the resolved
    # target (same symlink-aware behavior as the macOS .zshrc branch)
    h = fake_home
    real = h / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text("# existing\n", encoding="utf-8")
    (h / ".bashrc").symlink_to(real)
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr("sys.platform", "linux")
    install.install(h, window=262144, runner=FakeRun())
    assert "cc-continuity" in real.read_text(encoding="utf-8")
    assert (h / ".bashrc").is_symlink()
    assert not (h / ".zshrc").exists()
    manifest = _manifest(h)
    assert manifest["rc_file"] == str(real)
    assert manifest["platform"] == "linux"
    assert "zshrc" not in manifest


# --- uninstall (non-macOS) --------------------------------------------------


def test_uninstall_removes_block_from_rc_file(fake_home, monkeypatch):
    h = fake_home
    (h / ".bashrc").write_text("export FOO=1\n", encoding="utf-8")
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr("sys.platform", "linux")
    install.install(h, window=262144, runner=FakeRun())
    # the block landed in the bash rc (NOT .zshrc — macOS target)
    assert "cc-continuity" in (h / ".bashrc").read_text(encoding="utf-8")
    assert not (h / ".zshrc").exists()
    install.uninstall(h)
    text = (h / ".bashrc").read_text(encoding="utf-8")
    assert "export FOO=1" in text
    assert "cc-continuity" not in text
    assert (h / ".bashrc").exists()  # pre-existing file: never removed
