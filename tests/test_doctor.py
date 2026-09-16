"""doctor.py: PRD §16 health checks on a fake HOME."""

import dataclasses
import json
import os
import time

import pytest

from cc_continuity import doctor, install
from test_install import FakeRun


@pytest.fixture
def fake_home(tmp_path, monkeypatch, cc_home):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home / ".claude-continuity"))
    return home


def _which_ok(name):
    return {
        "tmux": "/usr/bin/tmux", "zsh": "/bin/zsh",
        "claude": "/usr/bin/claude",
    }.get(name, "/usr/bin/" + name)


def test_installed_home_all_green(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    # install() chmods the store 0700 (PRD 19) -> no store-perms warning
    assert not next(c for c in checks if c.name == "store-perms").warning
    names = {c.name for c in checks}
    assert {"python", "tmux", "zsh", "claude", "app", "symlink", "config",
            "settings-hooks", "statusline", "claude-md", "zshrc", "skills",
            "stale-locks", "tmp-residue", "errors-log"} <= names


def test_fresh_home_reports_red(fake_home):
    checks = doctor.run_doctor(fake_home, runner=FakeRun(), which=_which_ok)
    bad = {c.name for c in checks if not c.ok}
    assert {"app", "symlink", "config", "settings-hooks", "statusline",
            "claude-md", "zshrc", "skills"} <= bad


def test_old_claude_version_not_ok(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    checks = doctor.run_doctor(h, runner=FakeRun("1.9.0"), which=_which_ok)
    assert not next(c for c in checks if c.name == "claude").ok


def test_changed_claude_version_warns(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    checks = doctor.run_doctor(h, runner=FakeRun("2.2.0"), which=_which_ok)
    c = next(c for c in checks if c.name == "claude")
    assert c.ok and c.warning


def test_stale_lock_warns(fake_home, cc_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    proj = h / ".claude-continuity" / "projects" / "p-1"
    (proj / "sessions").mkdir(parents=True)
    (proj / "lock").write_text(json.dumps(
        {"pid": 999999, "ts": time.time() - 600, "rotation_id": None}),
        encoding="utf-8")
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    c = next(x for x in checks if x.name == "stale-locks")
    assert c.warning


def test_old_tmp_residue_deleted_and_warned(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    sess = h / ".claude-continuity" / "projects" / "p-1" / "sessions" / "001"
    sess.mkdir(parents=True)
    old = sess / "checkpoint.tmp-123"
    old.mkdir(parents=True)  # finalize creates a tmp DIRECTORY; doctor
    # skips non-dirs, so a plain file would make this test vacuous
    past = time.time() - 7200
    os.utime(old, (past, past))
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    c = next(x for x in checks if x.name == "tmp-residue")
    assert c.warning and not old.exists()


def test_errors_log_tail_reported(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    logs = h / ".claude-continuity" / "logs"
    logs.mkdir(parents=True)  # install does not create logs/; the hooks do
    (logs / "errors.log").write_text("line1\nline2\nline3\nline4\n",
                                     encoding="utf-8")
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    c = next(x for x in checks if x.name == "errors-log")
    assert c.warning
    # detail carries the last 3 lines (line2..line4); line1 is dropped
    assert "line4" in c.detail and "line1" not in c.detail


def test_render_and_json_shape(fake_home):
    h = fake_home
    install.install(h, window=262144, runner=FakeRun())
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    text = doctor.render(checks)
    assert "python" in text and "✓" in text
    for c in checks:
        d = dataclasses.asdict(c)
        assert {"name", "ok", "detail", "warning"} <= set(d)
