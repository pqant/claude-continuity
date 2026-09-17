"""uninstall(): PRD §17 — manifest-driven inverse, byte-for-byte (Gate 7)."""

import json

import pytest

from cc_continuity import install

from test_install import FakeRun  # same 10-line fixture pattern


@pytest.fixture
def fake_home(tmp_path, monkeypatch, cc_home):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


@pytest.fixture
def aliased_home(tmp_path, monkeypatch, cc_home):
    """Home under a symlinked directory component: raw path != realpath
    (the macOS /tmp -> /private/tmp shape that broke Gate 7, F-G7-1)."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    home = alias / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


@pytest.fixture
def seeded_home(fake_home):
    """Realistic pre-install state (the Gate 7 shape)."""
    h = fake_home
    (h / ".claude").mkdir()
    seed_settings = {
        "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95"},
        "statusLine": {"type": "command",
                       "command": "~/.claude/bin/claude-hud-launch.sh"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "curl -s https://ntfy.sh/x"}]}]},
    }
    (h / ".claude" / "settings.json").write_text(
        json.dumps(seed_settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (h / ".claude" / "CLAUDE.md").write_text("# graphify\n- skill\n",
                                             encoding="utf-8")
    dot = h / "dotfiles" / "zshrc"
    dot.parent.mkdir(parents=True)
    dot.write_text("export FOO=1\n", encoding="utf-8")
    (h / ".zshrc").symlink_to(dot)
    return h


def test_byte_for_byte_roundtrip_on_seeded_home(seeded_home):
    h = seeded_home
    # foreign skill present BEFORE install: must survive uninstall
    (h / ".claude" / "skills" / "graphify").mkdir(parents=True)
    (h / ".claude" / "skills" / "graphify" / "SKILL.md").write_text("x",
                                                                   encoding="utf-8")
    before = {
        "settings": (h / ".claude" / "settings.json").read_bytes(),
        "claude_md": (h / ".claude" / "CLAUDE.md").read_bytes(),
        "zshrc": (h / "dotfiles" / "zshrc").read_bytes(),
    }
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)
    assert (h / ".claude" / "settings.json").read_bytes() == before["settings"]
    assert (h / ".claude" / "CLAUDE.md").read_bytes() == before["claude_md"]
    assert (h / "dotfiles" / "zshrc").read_bytes() == before["zshrc"]
    assert (h / ".zshrc").is_symlink()
    assert not (h / ".local" / "bin" / "cc-continuity").exists()
    assert not (h / ".claude-continuity" / "app").exists()
    # skills: the six gone, foreign ones survive
    names = {p.name for p in (h / ".claude" / "skills").iterdir()}
    assert names == {"graphify"}


def test_reinstall_then_uninstall_restores_settings_byte_for_byte(seeded_home):
    """F-G7-2 regression — the Gate 7 flow itself: install ×2 then
    uninstall must restore settings.json byte-for-byte. The second
    install's manifest must carry over the first install's recorded user
    originals (statusLine, env_prev, allow_added, dirs_added)."""
    h = seeded_home
    before = (h / ".claude" / "settings.json").read_bytes()
    install.install(h, window=262144, runner=FakeRun())
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)
    after = (h / ".claude" / "settings.json").read_bytes()
    assert after == before
    s = json.loads(after)
    assert s["statusLine"]["command"] == "~/.claude/bin/claude-hud-launch.sh"
    assert "permissions" not in s


def test_reinstall_with_window_change_preserves_user_env_value(seeded_home):
    """F-G7-2 general case: user MAX_CONTEXT=262144 + --window 40000 on
    both installs; uninstall must restore the user's 262144 (not 40000)
    and drop the installer-written ACW key."""
    h = seeded_home
    install.install(h, window=40000, runner=FakeRun())
    install.install(h, window=40000, runner=FakeRun())
    install.uninstall(h)
    s = json.loads((h / ".claude" / "settings.json").read_text("utf-8"))
    assert s["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in s["env"]


def test_uninstall_removes_symlink_with_symlinked_path_component(aliased_home):
    """F-G7-1 regression: uninstall must still recognize (and remove) our
    ~/.local/bin symlink when the home path contains a symlinked
    component — same realpath-vs-prefix fix as the doctor check."""
    h = aliased_home
    install.install(h, window=262144, runner=FakeRun())
    link = h / ".local" / "bin" / "cc-continuity"
    assert link.is_symlink()
    install.uninstall(h)
    assert not link.is_symlink()


def test_created_files_removed_when_no_user_content(fake_home):
    h = fake_home
    store_proj = h / ".claude-continuity" / "projects" / "p-1"
    store_proj.mkdir(parents=True)
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)
    assert not (h / ".claude" / "settings.json").exists()
    assert not (h / ".claude" / "CLAUDE.md").exists()
    assert not (h / ".zshrc").exists()
    assert (h / ".claude-continuity" / "projects" / "p-1").is_dir()  # kept
    cfg = json.loads((h / ".claude-continuity" / "config.json")
                     .read_text(encoding="utf-8"))
    assert "install" not in cfg and "chained_statusline" not in cfg


def test_uninstall_idempotent(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)
    state1 = sorted(str(p) for p in h.rglob("*") if p.is_file())
    install.uninstall(h)  # second run: no-op, no error
    state2 = sorted(str(p) for p in h.rglob("*") if p.is_file())
    assert state1 == state2


def test_user_edits_between_install_and_uninstall_preserved(fake_home):
    h = fake_home
    (h / ".claude").mkdir()
    (h / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {}}, indent=2) + "\n", encoding="utf-8")
    (h / ".claude" / "CLAUDE.md").write_text("line one\n", encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    # user adds their own entry + their own CLAUDE.md line
    sp = h / ".claude" / "settings.json"
    s = json.loads(sp.read_text(encoding="utf-8"))
    s["hooks"]["Notification"] = [{"matcher": "", "hooks": [
        {"type": "command", "command": "user-notify"}]}]
    sp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n",
                  encoding="utf-8")
    cm = h / ".claude" / "CLAUDE.md"
    cm.write_text(cm.read_text(encoding="utf-8") + "user line\n",
                  encoding="utf-8")
    install.uninstall(h)
    s2 = json.loads(sp.read_text(encoding="utf-8"))
    assert s2["hooks"]["Notification"][0]["hooks"][0]["command"] == "user-notify"
    for event in ("SessionStart", "Stop"):
        assert event not in s2.get("hooks", {})
    # the upsert's blank separator line survives uninstall (one blank line
    # between the original content and the user's appended line)
    assert cm.read_text(encoding="utf-8") == "line one\n\nuser line\n"


def test_purge_removes_whole_store(fake_home):
    h = fake_home
    store_proj = h / ".claude-continuity" / "projects" / "p-1"
    store_proj.mkdir(parents=True)
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h, purge=True)
    assert not (h / ".claude-continuity").exists()


def test_foreign_local_bin_symlink_not_removed(fake_home):
    h = fake_home
    other = h / "other-tool"
    other.mkdir()
    (other / "cc-continuity").write_text("x", encoding="utf-8")
    (h / ".local" / "bin" / "cc-continuity").symlink_to(other / "cc-continuity")
    install.install(h, window=262144, runner=FakeRun())  # re-points it
    # now simulate a user who re-pointed it elsewhere after install:
    link = h / ".local" / "bin" / "cc-continuity"
    link.unlink()
    link.symlink_to(other / "cc-continuity")
    install.uninstall(h)
    assert link.is_symlink()  # user's symlink survived


def test_corrupt_config_blocks_uninstall(fake_home):
    h = fake_home
    store = h / ".claude-continuity"
    store.mkdir()
    (store / "config.json").write_text("{broken", encoding="utf-8")
    (h / ".claude").mkdir()
    (h / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    with pytest.raises(install.UninstallError, match="unreadable"):
        install.uninstall(h)
    assert (h / ".claude" / "settings.json").read_text() == "{}"  # untouched


def test_uninstall_without_manifest_still_removes_identifiable(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    cfg_path = h / ".claude-continuity" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.pop("install")  # simulate a lost manifest
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    install.uninstall(h)
    s = json.loads((h / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for event, entries in s.get("hooks", {}).items():
        assert all("cc-continuity" not in
                   json.dumps(e) for e in entries)
    assert "cc-continuity" not in \
        (h / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")


def test_zshrc_resolved_fallback_when_manifest_stale(fake_home):
    h = fake_home
    dot = h / "dots" / "zshrc"
    dot.parent.mkdir(parents=True)
    dot.write_text("a\n", encoding="utf-8")
    (h / ".zshrc").symlink_to(dot)
    install.install(h, window=262144, runner=FakeRun())
    # manifest points at the OLD resolved path after user moved the target:
    cfg_path = h / ".claude-continuity" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["install"]["zshrc"]["resolved"] = str(h / "gone" / "zshrc")
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (h / ".zshrc").unlink()
    (h / ".zshrc").symlink_to(dot)
    install.uninstall(h)
    assert dot.read_text(encoding="utf-8") == "a\n"


def test_config_left_with_only_defaults_kept_not_deleted(fake_home):
    h = fake_home
    store = h / ".claude-continuity"
    store.mkdir()
    (store / "config.json").write_text(json.dumps(
        {"rotate_ratio": 0.5}), encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)
    cfg = json.loads((store / "config.json").read_text(encoding="utf-8"))
    assert cfg["rotate_ratio"] == 0.5
    assert "install" not in cfg


def test_uninstall_keeps_projects_and_logs_dirs(fake_home):
    h = fake_home
    store = h / ".claude-continuity"
    (store / "projects" / "p-1").mkdir(parents=True)
    (store / "logs").mkdir(parents=True)
    (store / "logs" / "events.log").write_text("e\n", encoding="utf-8")
    install.install(h, window=262144, runner=FakeRun())
    install.uninstall(h)  # no purge: projects/ and logs/ survive
    assert (store / "projects" / "p-1").is_dir()
    assert (store / "logs" / "events.log").read_text(encoding="utf-8") == "e\n"


def test_symlink_zshrc_created_flag_not_set_for_real_symlink(seeded_home):
    manifest = install.install(seeded_home, window=262144, runner=FakeRun())
    # the symlink already pointed at a real file -> installer did not create it
    assert manifest["zshrc"]["created"] is False
