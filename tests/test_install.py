"""install.py: PRD §17 install into a fake HOME (never the real one)."""

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from cc_continuity import install, settings_merge as sm


@pytest.fixture
def fake_home(tmp_path, monkeypatch, cc_home):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


class FakeRun:
    """Stand-in for subprocess.run in prerequisite checks."""
    def __init__(self, version="2.1.209 (Claude Code)"):
        self.version = version
        self.calls = []
    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        class R: pass
        r = R(); r.stdout = self.version; r.stderr = ""; r.returncode = 0
        return r


def _settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_install_fresh_home_creates_all(fake_home):
    manifest = install.install(fake_home, window=262144, runner=FakeRun())
    h = fake_home
    s = _settings(h)
    assert set(s) == {"hooks", "statusLine", "env", "permissions"}
    assert len(s["hooks"]) == 10
    assert s["statusLine"]["command"].endswith("/cc-continuity statusline")
    assert s["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert (h / ".claude" / "CLAUDE.md").is_file()
    ztext = (h / ".zshrc").read_text(encoding="utf-8")
    assert "cc-continuity launch --" in ztext
    assert len(list((h / ".claude" / "skills").iterdir())) == 6
    link = h / ".local" / "bin" / "cc-continuity"
    assert link.is_symlink()
    launcher = Path(manifest["launcher"])
    assert launcher.is_file()
    cfg = json.loads((h / ".claude-continuity" / "config.json").read_text(encoding="utf-8"))
    assert cfg["window_tokens"] == 262144
    assert "install" in cfg


def test_hook_commands_use_absolute_launcher_path(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    s = _settings(fake_home)
    for event in sm.HOOK_EVENTS:
        cmd = s["hooks"][event][0]["hooks"][0]["command"]
        assert cmd == f"{fake_home}/.local/bin/cc-continuity hook {event}"


def test_launcher_stub_is_executable_and_points_at_app(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    launcher = fake_home / ".claude-continuity" / "app" / "bin" / "cc-continuity"
    mode = os.stat(launcher).st_mode
    assert mode & stat.S_IXUSR
    text = launcher.read_text(encoding="utf-8")
    # Absolute interpreter: `#!/usr/bin/env python3` broke the Phase 10
    # LaunchAgent — launchd's minimal PATH has no homebrew, so `env python3`
    # resolved to the system 3.9, which cannot parse `dict | None`
    # (TypeError at import → crash loop, watchdog never probed).
    assert text.startswith(f"#!{sys.executable}")
    assert "cc_continuity.cli import main" in text


def test_app_copy_has_no_pycache(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    app = fake_home / ".claude-continuity" / "app" / "cc_continuity"
    assert (app / "__init__.py").is_file()
    assert (app / "hooks" / "common.py").is_file()
    assert not list(app.rglob("__pycache__"))
    assert not list(app.rglob("*.pyc"))


def test_copy_app_refuses_self_copy(tmp_path, monkeypatch):
    # Bare `cc-continuity install` run from the installed app dir must raise,
    # not rmtree itself (Phase 26.1). Simulated with a copy of the package so
    # the dev tree is never at risk, even if the guard is missing.
    app_root = tmp_path / "app"
    pkg_copy = app_root / "cc_continuity"
    real_pkg = Path(install.__file__).resolve().parent
    shutil.copytree(real_pkg, pkg_copy,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(install, "__file__", str(pkg_copy / "install.py"))
    with pytest.raises(install.InstallError, match="dev tree"):
        install.copy_app(app_root)
    # The "installed" package must be untouched — no self-destruct.
    assert (pkg_copy / "install.py").is_file()


def test_realistic_seed_preserves_user_entries(fake_home):
    h = fake_home
    (h / ".claude").mkdir()
    seed = {
        "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95"},
        "statusLine": {"type": "command",
                       "command": "~/.claude/bin/claude-hud-launch.sh"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "curl -s https://ntfy.sh/example-topic"}]}]},
    }
    (h / ".claude" / "settings.json").write_text(
        json.dumps(seed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = install.install(h, window=262144, runner=FakeRun())
    s = _settings(h)
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"] == "curl -s https://ntfy.sh/example-topic"
    assert s["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "95"
    assert manifest["previous_status_line"]["command"] == \
        "~/.claude/bin/claude-hud-launch.sh"
    cfg = json.loads((h / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["chained_statusline"] == "~/.claude/bin/claude-hud-launch.sh"
    backups = list((h / ".claude").glob("settings.json.bak-*"))
    assert len(backups) == 1


def test_zshrc_symlink_written_to_resolved_target(fake_home):
    h = fake_home
    dot = h / "dotfiles" / "zshrc"
    dot.parent.mkdir(parents=True)
    dot.write_text("export FOO=1\n", encoding="utf-8")
    (h / ".zshrc").symlink_to(dot)
    install.install(h, window=262144, runner=FakeRun())
    assert (h / ".zshrc").is_symlink()
    target_text = dot.read_text(encoding="utf-8")
    assert target_text.startswith("export FOO=1\n")
    assert "# >>> cc-continuity >>>" in target_text
    manifest = json.loads((h / ".claude-continuity" / "config.json")
                          .read_text(encoding="utf-8"))["install"]
    assert manifest["zshrc"]["symlink"] is True
    assert manifest["zshrc"]["resolved"] == str(dot)


def test_claude_md_block_verbatim_and_existing_content_kept(fake_home):
    h = fake_home
    (h / ".claude").mkdir()
    (h / ".claude" / "CLAUDE.md").write_text(
        "# graphify\n- my skill line\n", encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    text = (h / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# graphify\n- my skill line\n\n")
    assert install.CLAUDE_MD_CONTENT in text
    assert text.count(install.CLAUDE_MD_BEGIN) == 1


def test_prereq_missing_claude_raises_and_writes_nothing(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: None if name == "claude"
                        else "/usr/bin/" + name)
    with pytest.raises(install.InstallError, match="claude CLI"):
        install.install(fake_home, window=262144, runner=FakeRun())
    assert not (fake_home / ".claude").exists()


def test_prereq_old_version_raises(fake_home):
    with pytest.raises(install.InstallError, match="2\\.1\\.209"):
        install.install(fake_home, window=262144, runner=FakeRun("1.0.0"))


def test_existing_non_symlink_local_bin_refused(fake_home):
    (fake_home / ".local" / "bin" / "cc-continuity").write_text("mine",
                                                                encoding="utf-8")
    with pytest.raises(install.InstallError, match="non-symlink"):
        install.install(fake_home, window=262144, runner=FakeRun())
    assert (fake_home / ".local" / "bin" / "cc-continuity").read_text() == "mine"


def test_reinstall_no_duplicates(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    install.install(fake_home, window=262144, runner=FakeRun())
    s = _settings(fake_home)
    for event in sm.HOOK_EVENTS:
        ours = [e for e in s["hooks"][event] if sm.is_ours(e)]
        assert len(ours) == 1
    ztext = (fake_home / ".zshrc").read_text(encoding="utf-8")
    assert ztext.count(install.ZSHRC_BEGIN) == 1
    # fresh home: install #1 CREATES settings.json (nothing to back up);
    # install #2 is the first backup. (Seeded home → one backup per
    # install; that shape is verified by Gate 7.)
    assert len(list((fake_home / ".claude").glob("settings.json.bak-*"))) == 1


def test_corrupt_settings_json_raises_and_file_untouched(fake_home):
    h = fake_home
    (h / ".claude").mkdir()
    (h / ".claude" / "settings.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(install.InstallError, match="invalid JSON"):
        install.install(h, window=262144, runner=FakeRun())
    assert (h / ".claude" / "settings.json").read_text() == "{oops"


def test_existing_config_json_user_keys_preserved(fake_home):
    h = fake_home
    store = h / ".claude-continuity"
    store.mkdir()
    (store / "config.json").write_text(
        json.dumps({"rotate_ratio": 0.58, "window_tokens": 111111}),
        encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    cfg = json.loads((store / "config.json").read_text(encoding="utf-8"))
    assert cfg["rotate_ratio"] == 0.58
    assert cfg["window_tokens"] == 262144  # explicit --window wins


def test_existing_store_projects_preserved(fake_home):
    h = fake_home
    proj = h / ".claude-continuity" / "projects" / "demoapp-a7e624b2" / "sessions"
    proj.mkdir(parents=True)
    (proj / "marker").write_text("keep", encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    assert (proj / "marker").read_text(encoding="utf-8") == "keep"


def test_symlink_repointed_when_target_changed(fake_home):
    link = fake_home / ".local" / "bin" / "cc-continuity"
    elsewhere = fake_home / "elsewhere"
    elsewhere.mkdir()
    link.symlink_to(elsewhere / "x")
    install.install(fake_home, window=262144, runner=FakeRun())
    assert link.is_symlink()
    assert link.resolve() == (fake_home / ".claude-continuity" / "app"
                              / "bin" / "cc-continuity").resolve()


def test_prerequisites_flag_missing_curl(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which",
                        lambda n: None if n == "curl" else "/usr/bin/fake")
    missing = install.check_prerequisites(fake_home, runner=FakeRun())
    assert "curl on PATH" in missing


def test_resolve_ntfy_flag_off_and_empty_disable():
    assert install.resolve_ntfy("off", is_tty=False) == ""
    assert install.resolve_ntfy("", is_tty=False) == ""


def test_resolve_ntfy_flag_url_validated():
    assert install.resolve_ntfy(
        "https://ntfy.sh/example-topic", is_tty=False
    ) == "https://ntfy.sh/example-topic"
    assert install.resolve_ntfy(
        "http://127.0.0.1:2001/topic", is_tty=False
    ) == "http://127.0.0.1:2001/topic"
    with pytest.raises(install.InstallError):
        install.resolve_ntfy("ntfy.sh/no-scheme", is_tty=False)


def test_resolve_ntfy_nontty_without_flag_untouched():
    assert install.resolve_ntfy(None, is_tty=False) is None


def test_resolve_ntfy_tty_prompt():
    assert install.resolve_ntfy(None, is_tty=True,
                                prompt=lambda *_: "") == ""
    assert install.resolve_ntfy(None, is_tty=True,
                                prompt=lambda *_: "https://ntfy.sh/t") == \
        "https://ntfy.sh/t"
    with pytest.raises(install.InstallError):
        install.resolve_ntfy(None, is_tty=True, prompt=lambda *_: "bad url")


def test_install_ntfy_flag_writes_config(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun(),
                    ntfy="https://ntfy.sh/example-topic")
    cfg = json.loads((fake_home / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["notify_endpoint"] == "https://ntfy.sh/example-topic"
    assert cfg["notify_enabled"] is True


def test_install_ntfy_empty_disables(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun(), ntfy="")
    cfg = json.loads((fake_home / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["notify_endpoint"] == ""
    assert cfg["notify_enabled"] is False


def test_install_ntfy_none_keeps_previous_value(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun(),
                    ntfy="https://ntfy.sh/first")
    install.install(fake_home, window=262144, runner=FakeRun())
    cfg = json.loads((fake_home / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["notify_endpoint"] == "https://ntfy.sh/first"


def test_managed_block_has_signal_guidance(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    text = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "cc-continuity signal needs-human" in text
    assert "cc-continuity signal done" in text
