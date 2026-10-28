"""Platform seam (hosting.py): kind detection + LaunchdHost delegation."""

import argparse
import json

import pytest
from cc_continuity import hosting


@pytest.mark.parametrize("osrel,procver,expected", [
    ("5.15.153.1-microsoft-standard-WSL2", "", "wsl2"),
    ("5.10.16.3-microsoft-standard-WSL1", "", "wsl1"),
    ("4.4.0-19041-Microsoft",
     "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com) "
     "#1-Microsoft Fri Mar 29 2021 00:00:00 PST", "wsl2"),  # old format -> assume wsl2
    ("6.8.0-generic", "Linux version 6.8.0-generic (buildd@lcy02) ", "linux"),
])
def test_kind_matrix(osrel, procver, expected):
    assert hosting.kind(osrelease=osrel, procversion=procver) == expected


def test_kind_darwin(monkeypatch):
    monkeypatch.setattr(hosting.sys, "platform", "darwin")
    assert hosting.kind() == "macos"


def test_launchd_host_delegates(monkeypatch):
    from cc_continuity import watchdogd
    calls = []
    monkeypatch.setattr(watchdogd, "install_plist",
                        lambda home, **kw: (calls.append((home, kw.get("runner"))), "/x.plist")[1])
    host = hosting.LaunchdHost(runner="SENTINEL_RUNNER")
    assert host.install("watchdog", "/h/u") == "/x.plist"
    assert calls == [("/h/u", "SENTINEL_RUNNER")]  # exact delegation, same args


def test_systemd_unit_render():
    u = hosting.SystemdHost().unit_render("watchdog", "/h/.local/bin/cc-continuity", "/h")
    assert all(s in u for s in ("[Unit]", "[Service]", "[Install]"))
    assert "ExecStart=/h/.local/bin/cc-continuity watchdog run" in u
    assert "Restart=on-failure" in u
    assert "WantedBy=default.target" in u
    u2 = hosting.SystemdHost().unit_render("watchdog-digest",
                                           "/h/.local/bin/cc-continuity", "/h")
    assert "ExecStart=/h/.local/bin/cc-continuity watchdog digest run-loop" in u2


def test_systemd_install_sequence(tmp_path, monkeypatch):
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0; stdout = "active"; stderr = ""
        return R()
    monkeypatch.setattr("shutil.which",
                        lambda n: "/h/.local/bin/cc-continuity"
                        if n == "cc-continuity" else None)
    host = hosting.SystemdHost(runner=runner)
    out = host.install("watchdog", tmp_path)
    assert out.endswith("cc-continuity-watchdog.service")
    unit = tmp_path / ".config/systemd/user/cc-continuity-watchdog.service"
    assert unit.is_file()
    joined = [" ".join(c) for c in calls]
    assert any("daemon-reload" in c for c in joined)
    assert any("enable --now" in c and "cc-continuity-watchdog.service" in c for c in joined)
    assert all("--user" in c for c in calls)  # never sudo, never system scope


# --- Task 5: select_host matrix + lifecycle routing ------------------------


@pytest.mark.parametrize("platform,systemd,expected", [
    ("darwin", False, hosting.LaunchdHost),
    ("linux", False, hosting.TmuxHost),      # WSL1 / bare Linux
    ("linux", True, hosting.SystemdHost),    # WSL2 with systemd
])
def test_select_host_matrix(monkeypatch, platform, systemd, expected):
    monkeypatch.setattr(hosting.sys, "platform", platform)
    monkeypatch.setattr(hosting, "systemd_running", lambda: systemd)
    assert type(hosting.select_host()) is expected


def test_power_plug_refused_off_macos(monkeypatch, capsys):
    monkeypatch.setattr(hosting.sys, "platform", "linux")
    import cc_continuity.cli as cli
    rc = cli.cmd_power_plug(type("A", (), {"power_command": "install",
                                           "home": None})())
    assert rc == 1
    assert "macOS-only" in capsys.readouterr().err


def _off_macos(monkeypatch):
    """Route selection away from launchd: WSL1, no systemd, bash rc."""
    monkeypatch.setattr(hosting, "kind", lambda *a, **k: "wsl1")
    monkeypatch.setattr(hosting, "systemd_running", lambda: False)
    monkeypatch.setenv("SHELL", "/bin/bash")


def _refusing_runner(calls):
    def runner(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 1
            stdout = ""
            stderr = ""
        return R()
    return runner


def test_watchdog_cli_routes_to_selected_host(cc_home, tmp_path, monkeypatch):
    """Off-macOS the CLI drives the selected host (TmuxHost), not the
    plist trio; the caller's runner is passed through."""
    import cc_continuity.cli as cli
    (cc_home / "config.json").write_text(
        json.dumps({"watchdog_enabled": True}), encoding="utf-8")
    _off_macos(monkeypatch)
    calls = []
    rc = cli.cmd_watchdog(argparse.Namespace(command="watchdog",
                                             watchdog_command="install"),
                          home=tmp_path, runner=_refusing_runner(calls))
    assert rc == 0
    assert (cc_home / "runtime" / "daemon-watchdog.json").is_file()
    assert any("new-session" in " ".join(c) and "cc-d-watchdog" in " ".join(c)
               for c in calls)
    rc_text = (tmp_path / ".bashrc").read_text(encoding="utf-8")
    assert "cc-d-watchdog" in rc_text  # self-heal line landed


def test_digest_cli_routes_to_selected_host(cc_home, tmp_path, monkeypatch):
    """The digest routes under the daemon name "watchdog-digest"."""
    import cc_continuity.cli as cli
    (cc_home / "config.json").write_text("{}", encoding="utf-8")
    _off_macos(monkeypatch)
    rc = cli.cmd_watchdog_digest(argparse.Namespace(command="watchdog",
                                                   digest_command="install"),
                                 home=tmp_path,
                                 runner=_refusing_runner([]))
    assert rc == 0
    assert (cc_home / "runtime" / "daemon-watchdog-digest.json").is_file()


# --- exact tmux targets (run #3 e2e 6a false-FAIL) -------------------------
# Bare "cc-d-watchdog" prefix-matches the surviving "cc-d-watchdog-digest"
# session; every daemon-session reference must use the exact "=<name>".


class _FakeR:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _with_state_file(tmp_path, monkeypatch):
    """ensure() no-ops without a state file — point it at one we create."""
    sp = tmp_path / "daemon-watchdog.json"
    sp.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hosting.daemon_supervisor, "state_path",
                        lambda name, home: sp)
    return sp


def test_tmux_ensure_absent_exact_target(tmp_path, monkeypatch):
    _with_state_file(tmp_path, monkeypatch)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return _FakeR(1)  # session absent
    host = hosting.TmuxHost(runner=runner)
    assert host.ensure("watchdog", tmp_path) == "cc-d-watchdog"
    assert ["tmux", "has-session", "-t", "=cc-d-watchdog"] in calls
    assert ["tmux", "new-session", "-d", "-s", "cc-d-watchdog",
            "-e", "CC_CONTINUITY_SUPERVISOR=1", "-c", str(tmp_path),
            "cc-continuity", "daemon", "run", "watchdog"] in calls


def test_tmux_ensure_healthy_exact_target(tmp_path, monkeypatch):
    _with_state_file(tmp_path, monkeypatch)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return _FakeR(0)  # alive AND marked -> healthy
    host = hosting.TmuxHost(runner=runner)
    host.ensure("watchdog", tmp_path)
    assert ["tmux", "has-session", "-t", "=cc-d-watchdog"] in calls
    assert ["tmux", "show-environment", "-t", "=cc-d-watchdog",
            "CC_CONTINUITY_SUPERVISOR"] in calls
    assert not any(c[1] == "new-session" for c in calls)


def test_tmux_ensure_collision_exact_kill(tmp_path, monkeypatch):
    _with_state_file(tmp_path, monkeypatch)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        # name taken (has-session 0) but no supervisor marker -> kill it
        return _FakeR(0 if cmd[1] == "has-session" else 1)
    host = hosting.TmuxHost(runner=runner)
    host.ensure("watchdog", tmp_path)
    assert ["tmux", "kill-session", "-t", "=cc-d-watchdog"] in calls
    assert any(c[1] == "new-session" for c in calls)


def test_tmux_stop_exact_target(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return _FakeR(0)
    host = hosting.TmuxHost(runner=runner)
    assert host.stop("watchdog", tmp_path)
    assert calls == [["tmux", "kill-session", "-t", "=cc-d-watchdog"]]


def test_rc_daemon_line_exact_target():
    assert hosting.rc_daemon_line("watchdog") == (
        "tmux has-session -t =cc-d-watchdog 2>/dev/null || "
        "cc-continuity daemon ensure cc-d-watchdog")
