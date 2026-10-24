"""Platform seam (hosting.py): kind detection + LaunchdHost delegation."""

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
