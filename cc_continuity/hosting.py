"""Daemon host seam (spec §7): platform detection + Host implementations.

``kind()`` classifies the host OS; ``select_host()`` (Task 5) picks the
daemon host — ``LaunchdHost`` (macOS, 1:1 delegation to the per-module
plist trio), ``SystemdHost`` (systemd running), ``TmuxHost`` (default:
WSL / bare Linux without systemd).

macOS behavior is byte-identical: on darwin, ``LaunchdHost`` delegates to
the exact existing functions with the caller's runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Protocol

from . import watchdogd, watchdog_digest

OSRELEASE_PATH = "/proc/sys/kernel/osrelease"
PROCVERSION_PATH = "/proc/version"


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def kind(osrelease: str | None = None, procversion: str | None = None) -> str:
    """Classify the host: "macos" | "wsl1" | "wsl2" | "linux".

    With no arguments the real system is classified: the darwin check
    runs first (``/proc`` does not exist on macOS), then the ``/proc``
    files are read. Explicit arguments simulate a foreign system and
    override the local platform (tests run on any host).

    Old-format WSL (osrelease without an explicit WSLN tag) is
    indistinguishable from the osrelease alone — the ``/proc/version``
    "Microsoft" fallback assumes wsl2; host selection only ever depends
    on ``systemd_running()``, so the assumption is behaviorally inert.
    """
    if osrelease is None and procversion is None:
        if sys.platform == "darwin":
            return "macos"
        osrelease = _read(OSRELEASE_PATH)
        procversion = _read(PROCVERSION_PATH)
    o = (osrelease or "").lower()
    if "wsl2" in o:
        return "wsl2"
    if "wsl1" in o:
        return "wsl1"
    if "microsoft" in (procversion or "").lower():
        return "wsl2"
    return "linux"


def systemd_running() -> bool:
    """True when a systemd user session is available (``/run/systemd/system``)."""
    return Path("/run/systemd/system").exists()


class Host(Protocol):
    """Daemon lifecycle over one hosting mechanism.

    ``name`` is ``"watchdog"`` or ``"watchdog-digest"``; ``home`` is the
    user's home directory (passed through to the underlying module
    unchanged).
    """

    def kind(self) -> str: ...

    def install(self, name: str, home: Path) -> str: ...

    def uninstall(self, name: str, home: Path) -> None: ...

    def start(self, name: str, home: Path) -> bool: ...

    def stop(self, name: str, home: Path) -> bool: ...

    def status(self, name: str, home: Path) -> str: ...


class LaunchdHost(Host):
    """macOS: 1:1 delegation to the existing per-module plist trio."""

    DAEMON_MODULES = {"watchdog": watchdogd, "watchdog-digest": watchdog_digest}

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def kind(self) -> str:
        return "launchd"

    def install(self, name: str, home: Path) -> str:
        return self.DAEMON_MODULES[name].install_plist(home, runner=self.runner)

    def uninstall(self, name: str, home: Path) -> None:
        self.DAEMON_MODULES[name].uninstall_plist(home, runner=self.runner)

    def start(self, name: str, home: Path) -> bool:
        return self.DAEMON_MODULES[name].start_plist(home, runner=self.runner)

    def stop(self, name: str, home: Path) -> bool:
        return self.DAEMON_MODULES[name].stop_plist(home, runner=self.runner)

    def status(self, name: str, home: Path) -> str:
        return self.DAEMON_MODULES[name].status_plist(home, runner=self.runner)


def select_host() -> Host:
    raise NotImplementedError("select_host is implemented in Task 5")
