"""Daemon supervisor (spec §7.3): the TmuxHost session command.

Runs ``cc-continuity daemon run <name>`` inside the daemon's tmux
session: spawns the child (long-running for the watchdog, the periodic
``digest run-loop`` for the digest — naively respawning the digest's
one-shot pass would hot-loop), respawns with capped backoff on exit,
and gives up after a crash loop (5 restarts in 60 s) with an ntfy
error. The supervisor's exit ends the tmux session (no remain-on-exit),
so ``tmux has-session`` is a complete liveness test.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import atomic, paths

#: Capped respawn backoff (seconds); index capped at the last entry.
BACKOFF_S = (1, 2, 5, 15)
#: Crash-loop guard: this many restarts within this window → give up.
CRASH_LIMIT = 5
CRASH_WINDOW_S = 60.0

#: The plist ProgramArguments tail per daemon (spec §7.3).
DAEMON_ARGV = {
    "watchdog": ("watchdog", "run"),
    "watchdog-digest": ("watchdog", "digest", "run-loop"),
}


def child_argv(name: str, launcher: Path) -> list[str]:
    if name not in DAEMON_ARGV:
        raise ValueError(f"unknown daemon name: {name!r}")
    return [str(launcher), *DAEMON_ARGV[name]]


def state_path(name: str, home: Path) -> Path:
    """``daemon-<name>.json`` in the store's runtime dir.

    ``home`` is for Host signature parity; the store root follows the
    ``CC_CONTINUITY_HOME`` / ``~/.claude-continuity`` convention.
    """
    return paths.runtime_dir() / f"daemon-{name}.json"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, **updates) -> None:
    data = _load_state(path)  # merge — never clobber (e.g. "installed")
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.atomic_write_json(path, data)


def _default_spawn(argv: list[str]):
    """Popen the child with ``CC_CONTINUITY_TMUX_BIN`` (absolute tmux
    path) — the launchd minimal-PATH bug must not resurface in tmux."""
    env = dict(os.environ)
    tmux = shutil.which("tmux")
    if tmux:
        env["CC_CONTINUITY_TMUX_BIN"] = tmux
    return subprocess.Popen(argv, env=env)


def _default_push(event: str, title: str, body: str) -> bool:
    """ntfy when notify is configured + enabled, else no-op False.
    Never raises."""
    try:
        from . import config, notify
        cfg = config.load_config()
        if not cfg.get("notify_enabled"):
            return False
        ep = cfg.get("notify_endpoint")
        if not ep:
            return False
        return notify.send(str(ep), title, body, "high",
                           event=event, wait=True)
    except Exception:
        return False


def run_supervisor(name: str, home: Path, *, spawn=None, sleep=None,
                   now=time.monotonic, push=None) -> int:
    """Supervise the daemon child until a crash loop or SIGTERM/SIGINT.

    Returns 0 on a clean stop, 1 after giving up on a crash loop.
    Signal handlers are installed only on the default (production)
    path — injected ``spawn``/``sleep`` (tests) manage lifecycle
    themselves.
    """
    if spawn is None:
        spawn = _default_spawn
    if sleep is None:
        sleep = time.sleep
    if push is None:
        push = _default_push

    launcher = shutil.which("cc-continuity") \
        or (Path.home() / ".local" / "bin" / "cc-continuity")
    argv = child_argv(name, launcher)
    sp = state_path(name, home)

    stopped = threading.Event()
    slot = {"child": None}

    def _on_signal(signum, frame):
        child = slot["child"]
        if child is not None:
            child.terminate()
        stopped.set()

    if spawn is _default_spawn:
        # production path: forward SIGTERM/SIGINT to the child (it
        # exits, wait() below returns, and the supervisor exits 0 —
        # session ends ⇔ supervisor is gone)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass  # not the main thread — the caller manages stop

    restarts: list[float] = []
    while True:
        child = spawn(argv)
        slot["child"] = child
        pid = getattr(child, "pid", None)
        if pid is not None:
            _save_state(sp, child_pid=pid)
        child.wait()  # respawn on ANY exit (the child is the daemon)
        slot["child"] = None
        if stopped.is_set():
            return 0
        restarts.append(now())
        cutoff = now() - CRASH_WINDOW_S
        restarts = [t for t in restarts if t >= cutoff]
        _save_state(sp, restarts=restarts)
        if len(restarts) >= CRASH_LIMIT:
            push("daemon-crashloop",
                 f"cc-continuity {name}: crash loop",
                 f"{name} restarted {len(restarts)}x in "
                 f"{CRASH_WINDOW_S:.0f} s — supervisor gave up; "
                 "run `cc-continuity doctor`")
            return 1
        sleep(BACKOFF_S[min(len(restarts) - 1, len(BACKOFF_S) - 1)])
