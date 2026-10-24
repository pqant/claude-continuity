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
