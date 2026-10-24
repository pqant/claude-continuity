"""Daemon host seam (spec §7): platform detection + Host implementations.

``kind()`` classifies the host OS; ``select_host()`` (Task 5) picks the
daemon host — ``LaunchdHost`` (macOS, 1:1 delegation to the per-module
plist trio), ``SystemdHost`` (systemd running), ``TmuxHost`` (default:
WSL / bare Linux without systemd).

macOS behavior is byte-identical: on darwin, ``LaunchdHost`` delegates to
the exact existing functions with the caller's runner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from . import atomic, config, daemon_supervisor, managed, watchdogd, \
    watchdog_digest

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


class SystemdHost(Host):
    """systemd user units (no sudo, never system scope).

    ``ExecStart`` mirrors the plist's ``ProgramArguments`` tail; the
    digest runs the periodic ``run-loop`` (its plist is StartInterval,
    not KeepAlive — a one-shot child would hot-loop).
    """

    # the plist ProgramArguments tail per daemon (spec §7.3)
    DAEMON_ARGS = {"watchdog": "watchdog run",
                   "watchdog-digest": "watchdog digest run-loop"}

    def __init__(self, runner=subprocess.run, which=None):
        self.runner = runner
        self._which = which  # resolved at call time when None

    def _resolve_which(self):
        # resolved at call time so tests can monkeypatch shutil.which
        return self._which if self._which is not None else shutil.which

    def kind(self) -> str:
        return "systemd"

    def unit_name(self, name: str) -> str:
        return f"cc-continuity-{name}.service"

    def unit_path(self, home: Path | str, name: str) -> Path:
        return Path(home) / ".config" / "systemd" / "user" / self.unit_name(name)

    def unit_render(self, name: str, binary: str, home: Path | str) -> str:
        logs = Path(home) / ".claude-continuity" / "logs" / f"{name}.out"
        return (
            "[Unit]\n"
            f"Description=cc-continuity {name} daemon\n"
            "\n"
            "[Service]\n"
            f"ExecStart={binary} {self.DAEMON_ARGS[name]}\n"
            "Restart=on-failure\n"
            f"StandardOutput=append:{logs}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _systemctl(self, *args: str) -> bool:
        try:
            proc = self.runner(["systemctl", "--user", *args],
                               capture_output=True, timeout=30)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def install(self, name: str, home: Path | str) -> str:
        which = self._resolve_which()
        binary = which("cc-continuity")
        if binary is None:
            raise watchdogd.WatchdogError("cc-continuity not on PATH (install first)")
        unit = self.unit_path(home, name)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(self.unit_render(name, binary, Path(home)), encoding="utf-8")
        self._systemctl("daemon-reload")
        self._systemctl("enable", "--now", unit.name)
        return str(unit)

    def uninstall(self, name: str, home: Path | str) -> None:
        unit = self.unit_path(home, name)
        self._systemctl("disable", "--now", unit.name)
        unit.unlink(missing_ok=True)
        self._systemctl("daemon-reload")

    def start(self, name: str, home: Path | str) -> bool:
        return self._systemctl("start", self.unit_name(name))

    def stop(self, name: str, home: Path | str) -> bool:
        return self._systemctl("stop", self.unit_name(name))

    def status(self, name: str, home: Path | str) -> str:
        unit = self.unit_path(home, name)
        if not unit.is_file():
            return "not installed"
        try:
            proc = self.runner(["systemctl", "--user", "show", unit.name,
                                "-p", "ActiveState", "--value"],
                               capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return "not installed"
        out = proc.stdout
        if isinstance(out, bytes):  # fakes may answer with str
            out = out.decode(errors="replace")
        return (out or "").strip() or "unknown"


def rc_daemon_line(name: str) -> str:
    """The rc self-heal line for one daemon (spec §7.4).

    Both references use the SESSION name; ``daemon ensure`` normalizes
    the leading ``cc-d-`` back to the daemon name.
    """
    s = f"cc-d-{name}"
    return (f"tmux has-session -t {s} 2>/dev/null || "
            f"cc-continuity daemon ensure {s}")


def rc_block_content(lines: list[str]) -> str:
    """The rc block's body: the claude() wrapper + daemon self-heal lines."""
    from .install import ZSHRC_CONTENT  # lazy: T6 makes install import hosting
    return ZSHRC_CONTENT + "\n".join(lines)


def rc_file_for(home: Path) -> Path:
    """The shell rc file holding the daemon self-heal block.

    Non-macOS: the manifest's ``rc_file`` (written by the installer);
    macOS fallback: the manifest's zshrc resolved path; no manifest
    (pre-install, or T4 tests that run before the installer branch):
    the login shell's rc by ``$SHELL`` basename (bash → ``.bashrc``,
    zsh → ``.zshrc``).
    """
    home = Path(home)
    manifest = config.load_config().get("install")
    if not isinstance(manifest, dict):
        manifest = {}
    if kind() != "macos":
        rc = manifest.get("rc_file")
        if rc:
            return Path(rc)
    zshrc = (manifest.get("zshrc") or {}).get("resolved")
    if zshrc:
        return Path(zshrc)
    shell = (os.environ.get("SHELL") or "/bin/bash").rsplit("/", 1)[-1]
    return home / (".zshrc" if shell == "zsh" else ".bashrc")


def _rc_daemon_lines(text: str) -> list[str]:
    """Daemon self-heal lines currently in the rc block ([] if none)."""
    from .install import ZSHRC_BEGIN, ZSHRC_CONTENT, ZSHRC_END
    lines = text.split("\n")
    if ZSHRC_BEGIN not in lines or ZSHRC_END not in lines:
        return []
    i, j = lines.index(ZSHRC_BEGIN), lines.index(ZSHRC_END)
    if j <= i:
        return []
    body = lines[i + 1: j]
    wrapper = ZSHRC_CONTENT.rstrip("\n").split("\n")
    if body[: len(wrapper)] == wrapper:
        body = body[len(wrapper):]
    return [l for l in body if l.strip()]


class TmuxHost(Host):
    """WSL / bare Linux: the daemon lives in a detached tmux session.

    The session command is ``cc-continuity daemon run <name>`` (the
    daemon_supervisor); when the supervisor exits the session ends (no
    remain-on-exit), so ``tmux has-session`` is a complete liveness
    test. ``new-session -e`` needs tmux ≥ 3.2 (Ubuntu 22.04 ships 3.2a
    — documented in docs/WINDOWS.md, Task 8).
    """

    MARKER = "CC_CONTINUITY_SUPERVISOR"

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def kind(self) -> str:
        return "tmux"

    def session(self, name: str) -> str:
        return f"cc-d-{name}"

    def _run(self, *args: str):
        return self.runner(["tmux", *args], capture_output=True, timeout=15)

    def _has_session(self, session: str) -> bool:
        return self._run("has-session", "-t", session).returncode == 0

    def _marked(self, session: str) -> bool:
        """LIVE marker check: a state-file marker cannot see a
        user-created foreign session that owns the name."""
        proc = self._run("show-environment", "-t", session, self.MARKER)
        return proc.returncode == 0

    def ensure(self, name: str, home: Path) -> str:
        home = Path(home)
        session = self.session(name)
        if not daemon_supervisor.state_path(name, home).is_file():
            return session  # not installed here — no-op
        if self._has_session(session):
            if self._marked(session):
                return session  # healthy — do nothing
            self._run("kill-session", "-t", session)  # name collision
        # -s (not -t: on real tmux new-session, -t is a GROUP name and
        # errors with a command); a racing duplicate creation fails
        # clean ("duplicate session") and never replaces an existing one
        self._run("new-session", "-d", "-s", session,
                  "-e", f"{self.MARKER}=1", "-c", str(home),
                  "cc-continuity", "daemon", "run", name)
        return session

    def install(self, name: str, home: Path) -> str:
        home = Path(home)
        sp = daemon_supervisor.state_path(name, home)
        daemon_supervisor._save_state(sp, installed=True,
                                      session=self.session(name))
        self.ensure(name, home)
        self._rc_upsert(name, home, add=True)
        return self.session(name)

    def uninstall(self, name: str, home: Path) -> None:
        home = Path(home)
        self.stop(name, home)
        daemon_supervisor.state_path(name, home).unlink(missing_ok=True)
        self._rc_upsert(name, home, add=False)

    def start(self, name: str, home: Path) -> bool:
        self.ensure(name, home)
        return self._has_session(self.session(name))

    def stop(self, name: str, home: Path) -> bool:
        return self._run("kill-session",
                         "-t", self.session(name)).returncode == 0

    def status(self, name: str, home: Path) -> str:
        session = self.session(name)
        if not self._has_session(session):
            return "not running"
        data = daemon_supervisor._load_state(
            daemon_supervisor.state_path(name, Path(home)))
        pid = data.get("child_pid")
        return f"{session} (pid {pid})" if pid else session

    def _rc_upsert(self, name: str, home: Path, add: bool) -> None:
        from .install import ZSHRC_BEGIN, ZSHRC_END  # lazy (T6: import cycle)
        rc = rc_file_for(home)
        text = rc.read_text(encoding="utf-8") if rc.is_file() else ""
        lines = _rc_daemon_lines(text)
        line = rc_daemon_line(name)
        if add:
            if line not in lines:
                lines.append(line)
        else:
            lines = [l for l in lines if l != line]
            # a bare line outside the block (file predating the managed
            # block) must also leave the rc file
            text = "\n".join(l for l in text.split("\n") if l != line)
        rc.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_text(
            rc, managed.upsert_block(text, ZSHRC_BEGIN, ZSHRC_END,
                                     rc_block_content(lines)))


def select_host(runner=subprocess.run) -> Host:
    """Pick the daemon host (spec §7): macOS → launchd; a running systemd
    → user units; otherwise tmux (WSL1 / bare Linux without systemd).

    ``runner`` is passed to the host's constructor: the CLI forwards its
    own (so macOS stays byte-identical and tests inject a fake), the
    zero-arg form (cmd_daemon) uses the real subprocess.run.
    """
    if kind() == "macos":
        return LaunchdHost(runner=runner)
    if systemd_running():
        return SystemdHost(runner=runner)
    return TmuxHost(runner=runner)
