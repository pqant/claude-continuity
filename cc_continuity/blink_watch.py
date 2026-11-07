"""Session-resident waiting-blink poller (live bug 2026-11-07).

AskUserQuestion sat unanswered for 46 minutes in a mode-0 session and
the tab never blinked: the Blinker only ran inside the mode-1 pane
animator, and CC does not invoke the status line while it waits on a
question, so the per-window data file stayed marker-free. This poller
runs beside the session (spawned from SessionStart, independent of
Notification/Stop hooks: a blocking question never lets those fire in
time) and drives one Blinker from two sources: the pane's input-box
region (selection prompts) and the ledger's unconsumed ``needs_human``
reason (so ``signal needs-human`` blinks without a status line call).

Exit conditions (all silent): the session state leaves the live set
(rotation takes over), the capture fails ``_consec_fails`` times in a
row (pane/window died), or the 12 h lifetime cap is reached. A SIGTERM
at respawn may strand a WAIT; the Blinker startup sweep heals that on
the next animator/poller start (same treatment as a crash).

Store schema is the animator's (``blink-<window_id>-blink_name`` via
``progress_pane._read_pane_record``); the modes are mutually exclusive
(spawn skips mode 1), so the two writers can never collide.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import blink, config, ledger, paths, progress_pane

#: Only these state.json values keep the poller alive; anything else
#: (rotation handed over, session ended) is a silent quit.
LIVE_STATES = ("ACTIVE", "BOOTSTRAPPING")

_LIFETIME_SECONDS = 12 * 3600
_POLL_SECONDS = 1.0

_MISSING = object()


def _command() -> list[str]:
    exe = shutil.which("cc-continuity")
    return [exe] if exe else [sys.executable, "-m", "cc_continuity"]


def _make_store(window_id: str):
    """The animator's blink-store shape, same keys, one writer per mode."""
    def store(key, value=_MISSING):
        try:
            rec = progress_pane._read_pane_record()
            full = f"blink-{window_id}-{key}"
            if value is _MISSING:
                return rec.get(full)
            if value is None:
                rec.pop(full, None)
            else:
                rec[full] = value
            progress_pane._write_pane_record(rec)
        except Exception:
            return None
        return None
    return store


def _default_state(session_dir) -> str | None:
    """The ``state`` value of ``state.json``; None (never raises) if unreadable."""
    try:
        rec = json.loads((Path(str(session_dir)) / "state.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec.get("state") if isinstance(rec, dict) else None


def watch(session_dir, *, pane_id: str, window_id: str, cfg: dict,
          _runner=subprocess.run, _bin_=None, _tty_open=open,
          _now=time.time, _sleep=time.sleep, _ticks=None, _blink=None,
          _state=None, _consec_fails: int = 6) -> None:
    """Poll one session's pane until a quit condition; never raises.

    One capture per tick (~1 s): the region rule keeps scrollback out,
    so a footer quoted in agent chat above the box cannot strand ASK.
    A failed capture keeps the last region cache (tmux stalls happen
    mid-wait); ``_consec_fails`` consecutive failures mean the pane is
    gone and the poller quits.
    """
    blink_mod = _blink if _blink is not None else blink
    bin_ = (_bin_ if _bin_ is not None
            else os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux")
    state_fn = _state if _state is not None else _default_state
    try:
        blinker = blink_mod.Blinker(window_id, cfg,
                                    store=_make_store(window_id),
                                    runner=_runner, bin_=bin_,
                                    tty_open=_tty_open)
    except Exception:
        return
    start = _now()
    fails = 0
    pane_text: str | None = None
    i = 0
    try:
        while True:
            t = _now()
            if state_fn(session_dir) not in LIVE_STATES:
                return
            if t - start >= _LIFETIME_SECONDS:
                return
            try:
                cap = blink_mod.capture_pane(_runner, bin_, target=pane_id)
            except Exception:
                cap = None
            if cap is None:
                fails += 1
                if fails >= _consec_fails:
                    return
            else:
                fails = 0
                region = blink_mod.input_box_region_from_lines(
                    cap.splitlines())
                pane_text = None if region is None else "\n".join(region)
            try:
                blocked = ledger.blocked_reason(session_dir)
            except Exception:
                blocked = None
            text = blink_mod.MARKER_BLOCKED if blocked else ""
            blinker.on_text(text, t, pane_text=pane_text)
            i += 1
            if _ticks is not None and i >= _ticks:
                return
            _sleep(_POLL_SECONDS)
    except Exception:
        return
    finally:
        try:
            blinker.on_exit()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def spawn(session_dir, *, _runner=subprocess.run, _popen=subprocess.Popen,
          _kill=os.kill, _alive=_pid_alive) -> int | None:
    """Spawn the poller detached for THIS session's window; None = skip.

    Called from SessionStart (hook env carries $TMUX/$TMUX_PANE). Skips
    silently without a tmux env, when blink is disabled, or in mode 1
    (the animator owns the blink there). Singleton per window via
    ``runtime/blink-watch-<window_id>.pid`` ("pid nnn"); a live stale
    poller is SIGTERMed first (its ``on_exit`` restores the window).
    Never raises: a failed spawn only loses the blink, never the hook.
    """
    try:
        if not os.environ.get("TMUX"):
            return None
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return None
        cfg = config.load_config()
        if not config.blink_enabled(cfg) or config.progress_mode(cfg) == 1:
            return None
        bin_ = os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"
        window = blink._tmux(_runner, bin_, ["display-message", "-p",
                                             "-t", pane, "#{window_id}"])
        if not window:
            return None
        pidfile = paths.runtime_dir() / f"blink-watch-{window}.pid"
        try:
            old = pidfile.read_text(encoding="utf-8").strip()
            if old.startswith("pid "):
                pid = int(old.split()[1])
                if _alive(pid):
                    _kill(pid, signal.SIGTERM)
        except (OSError, ValueError, IndexError):
            pass
        cmd = _command() + ["blink-watch", "--pane", pane,
                            "--window", window,
                            "--session-dir", str(session_dir)]
        log_path = paths.logs_dir() / "blink-watch.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as logf:
                proc = _popen(cmd, stdin=subprocess.DEVNULL, stdout=logf,
                              stderr=logf, start_new_session=True)
        except OSError:
            return None
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(f"pid {proc.pid}\n", encoding="utf-8")
        except OSError:
            pass
        return proc.pid
    except Exception:
        return None
