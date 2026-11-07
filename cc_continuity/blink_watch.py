"""Session-resident waiting-blink poller (live bug 2026-11-07).

AskUserQuestion sat unanswered for 46 minutes in a mode-0 session and
the tab never blinked: the Blinker only ran inside the mode-1 pane
animator, and CC does not invoke the status line while it waits on a
question, so the per-window data file stayed marker-free. This poller
runs beside the session (spawned from SessionStart, independent of
Notification/Stop hooks: a blocking question never lets those fire in
time) and drives one Blinker from three sources: the pane's input-box
region (selection prompts), the ledger's unconsumed ``needs_human``
reason (so ``signal needs-human`` blinks without a status line call),
and the Stop hook's ``reply_ready`` entry (turn ended waiting for the
user; cleared by focus dwell or the reply fade).

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


def _default_client_ttys(window_id: str, _runner, bin_) -> set:
    """Client ttys attached to this window (focus-comparison set)."""
    return set(blink.list_client_ttys(_runner, bin_, window_id))


def _default_frontmost_tty():
    """iTerm2's frontmost session tty (AppleScript); None off-iTerm2.

    tmux never sees an iTerm2 tab click; the frontmost session's tty is
    the only reliable "the user is looking at this tab" signal. A
    non-iTerm2 context yields None, so the reply blink self-fades after
    ``_reply_fade`` seconds instead of hanging on never-arriving focus.
    """
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "iTerm2" to get tty of current session '
             'of current window'],
            capture_output=True, text=True, timeout=2.0)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def watch(session_dir, *, pane_id: str, window_id: str, cfg: dict,
          _runner=subprocess.run, _bin_=None, _tty_open=open,
          _now=time.time, _sleep=time.sleep, _ticks=None, _blink=None,
          _state=None, _consec_fails: int = 6,
          _frontmost_tty=None, _client_ttys=None,
          _dwell_ticks: int = 3, _reply_fade: float = 600.0,
          _focus_log=blink._debug_line) -> None:
    """Poll one session's pane until a quit condition; never raises.

    One capture per tick (~1 s): the region rule keeps scrollback out,
    so a footer quoted in agent chat above the box cannot strand ASK.
    A failed capture keeps the last region cache (tmux stalls happen
    mid-wait); ``_consec_fails`` consecutive failures mean the pane is
    gone and the poller quits.

    Reply-ready blink (feature 2026-11-07): an unconsumed
    ``reply_ready`` ledger entry (Stop hook: turn ended waiting for the
    user) blinks the tab WITHOUT needing status line renders. Replying
    is not required; a later main-session user prompt also consumes the
    entry.

    Focus dwell (user directive 2026-11-07, v2): EVERY blink source
    (needs_human, selection dialog, reply) falls silent once the user
    looks at this window's tab for ``_dwell_ticks`` consecutive ticks
    (2-3 s "I've seen it"); a changed situation (new reason, new dialog,
    fresh reply) re-arms. The reply additionally self-fades after
    ``_reply_fade`` s in non-focusable contexts; blocking states keep
    blinking there (a genuine wait must not strand unseen).
    """
    blink_mod = _blink if _blink is not None else blink
    bin_ = (_bin_ if _bin_ is not None
            else os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux")
    state_fn = _state if _state is not None else _default_state
    reply_on = config.blink_reply(cfg)
    store = _make_store(window_id)
    clients_fn = (_client_ttys if _client_ttys is not None
                  else (lambda: _default_client_ttys(window_id, _runner, bin_)))
    try:
        blinker = blink_mod.Blinker(window_id, cfg,
                                    store=store,
                                    runner=_runner, bin_=bin_,
                                    tty_open=_tty_open)
        # focus-dwell suppression (user directive 2026-11-07): every blink
        # source falls silent once the user looks at the tab. The watcher
        # itself performs no I/O: focus rides in on the injected seams and
        # None (the default) disables dwell entirely.
        watcher = blink.FocusWatcher(store, frontmost=_frontmost_tty,
                                     clients=clients_fn,
                                     dwell_ticks=_dwell_ticks,
                                     label=window_id or "",
                                     log=_focus_log)
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
            reply = None
            if reply_on:
                try:
                    reply = ledger.reply_pending(session_dir)
                except Exception:
                    reply = None
            if reply is not None and t - reply > _reply_fade:
                reply = None  # unfocusable context: fade, never strand
            parts = []
            if blocked:
                parts.append(blink_mod.MARKER_BLOCKED)
            if reply is not None:
                parts.append(blink_mod.MARKER_REPLY)
            text = " ".join(parts)
            # composite key (FocusWatcher): blocked reason, reply ts and the
            # pane region. A changed key (new needs-human reason, new
            # dialog, fresh reply) reads as unread and re-arms the blink
            # even while the tab stays focused; per-part repr keeps reason
            # and region text from ever colliding across the join.
            key = "|".join(repr(p) for p in
                           (blocked,
                            repr(reply) if reply is not None else "",
                            pane_text) if p)
            on_read = ((lambda _key: ledger.mark_reply_read(session_dir))
                       if reply is not None else None)
            try:
                suppress = watcher.tick(t, key, on_read)
            except Exception:
                suppress = False
            if suppress:
                blinker.on_text("", t, pane_text=None)
            else:
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
