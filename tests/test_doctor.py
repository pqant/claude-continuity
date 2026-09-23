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


def test_symlink_check_ok_with_symlinked_path_component(aliased_home):
    """F-G7-1 regression: home path with a symlinked component must not
    break the realpath-vs-prefix comparison (raw /alias/ vs resolved)."""
    h = aliased_home
    install.install(h, window=262144, runner=FakeRun())
    checks = doctor.run_doctor(h, runner=FakeRun(), which=_which_ok)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert os.path.realpath(str(h / ".local" / "bin" / "cc-continuity")) \
        != str(h / ".local" / "bin" / "cc-continuity")  # alias is real


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


class NotifyRun(FakeRun):
    """FakeRun that also plays the notify curl call."""
    def __init__(self, rc=0):
        super().__init__()
        self.rc = rc
        self.notify_cmd = None
    def __call__(self, cmd, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "curl":
            self.notify_cmd = cmd
            class R: pass
            r = R(); r.stdout = ""; r.stderr = ""; r.returncode = self.rc
            return r
        return super().__call__(cmd, **kw)


def _enable_notify(home):
    cfg_path = home / ".claude-continuity" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.update({"notify_enabled": True,
                "notify_endpoint": "https://ntfy.sh/example-topic"})
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def test_doctor_curl_check(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    by = {c.name: c for c in
          doctor.run_doctor(fake_home, runner=FakeRun(),
                            which=lambda n: "/usr/bin/fake")}
    assert by["curl"].ok is True
    by = {c.name: c for c in
          doctor.run_doctor(fake_home, runner=FakeRun(),
                            which=lambda n:
                                None if n == "curl" else "/usr/bin/fake")}
    assert by["curl"].ok is False
    assert by["curl"].detail == "not on PATH"


def test_doctor_notify_not_configured_is_not_a_failure(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    by = {c.name: c for c in
          doctor.run_doctor(fake_home, runner=FakeRun(),
                            which=lambda n: "/usr/bin/fake")}
    assert by["notify"].ok is True
    assert by["notify"].detail == "not configured (disabled)"


def test_doctor_notify_sends_test_message_when_configured(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    _enable_notify(fake_home)
    fake = NotifyRun()
    by = {c.name: c for c in
          doctor.run_doctor(fake_home, runner=fake,
                            which=lambda n: "/usr/bin/fake")}
    assert by["notify"].ok is True
    assert by["notify"].detail == "https://ntfy.sh/example-topic"
    assert fake.notify_cmd is not None
    assert "Title: 🔔 cc-continuity test" in fake.notify_cmd


def test_doctor_notify_failed_test_send_is_not_ok(fake_home):
    install.install(fake_home, window=262144, runner=FakeRun())
    _enable_notify(fake_home)
    by = {c.name: c for c in
          doctor.run_doctor(fake_home, runner=NotifyRun(rc=7),
                            which=lambda n: "/usr/bin/fake")}
    assert by["notify"].ok is False
    assert "test send failed" in by["notify"].detail


# --- Phase 10: watchdog check ------------------------------------------------


def _write_cfg(home, **extra):
    store = home / ".claude-continuity"
    store.mkdir(parents=True, exist_ok=True)
    (store / "config.json").write_text(
        json.dumps({"window_tokens": 262144, **extra}), encoding="utf-8")


def test_watchdog_check_disabled_ok(fake_home):
    checks = doctor.run_doctor(fake_home, runner=FakeRun(),
                               which=_which_ok)
    wd = next(c for c in checks if c.name == "watchdog")
    assert wd.ok and "disabled" in wd.detail


def test_watchdog_check_enabled_not_installed(fake_home):
    _write_cfg(fake_home, watchdog_enabled=True)
    checks = doctor.run_doctor(fake_home, runner=FakeRun(),
                               which=_which_ok)
    wd = next(c for c in checks if c.name == "watchdog")
    assert not wd.ok and "watchdog install" in wd.detail


def test_watchdog_check_enabled_binary_missing(fake_home, tmp_path):
    from cc_continuity import watchdogd
    _write_cfg(fake_home, watchdog_enabled=True)
    watchdogd.install_plist(fake_home, runner=FakeRun(),
                            which=lambda name:
                            "/nonexistent-cc-continuity-xyz")
    checks = doctor.run_doctor(fake_home, runner=FakeRun(),
                               which=_which_ok)
    wd = next(c for c in checks if c.name == "watchdog")
    assert not wd.ok and "watchdog install" in wd.detail


def test_watchdog_check_enabled_loaded_ok(fake_home, tmp_path):
    from cc_continuity import watchdogd
    fakebin = tmp_path / "cc-continuity-bin"
    fakebin.write_text("#!/bin/sh\n")
    _write_cfg(fake_home, watchdog_enabled=True)
    watchdogd.install_plist(fake_home, runner=FakeRun(),
                            which=lambda name: str(fakebin))
    checks = doctor.run_doctor(fake_home, runner=FakeRun(),
                               which=_which_ok)
    wd = next(c for c in checks if c.name == "watchdog")
    assert wd.ok and "loaded" in wd.detail
