"""Waiting-blink primitives (issue #11).

Thin, self-contained layer: the OSC 6 tab-color escape sequences (spike
proven on iTerm2 3.7.6 and Terminal.app), the gate markers the blink
reacts to, and the tmux/tty helpers the ``Blinker`` state machine uses.

Import direction rule: ``progress_pane`` and ``statusline`` import from
here; this module only leans on leaf modules (``config``, ``paths``) and
never on ``progress_pane`` or ``statusline``, so a blink failure can
never drag the animator down.
"""

import re
import subprocess
import time

from . import config, paths

OSC6_RESET = b"\x1b]6;1;bg;*;default\x07"

RED_FILL = (235, 20, 20)
BLACK_FILL = (0, 0, 0)

# Single source of truth for the gate markers. statusline.py composes its
# suffixes from these constants (composing its own leading spaces) so the
# writer and the gate can never drift apart silently.
MARKER_BLOCKED = "⛔ BLOCKED"
MARKER_ASK = "⚠ ASK"
MARKER_REPLY = "🕐 REPLY"
GATE_MARKERS = (MARKER_BLOCKED, MARKER_ASK, MARKER_REPLY)

# "User input needed" detection from raw pane text (live bug 2026-11-07:
# AskUserQuestion sat unanswered for 46 min without a blink). Permission
# prompts match the exact wording only, so ordinary chat containing
# "allow"/"yes" can never strand the marker; selection prompts (permission
# AND AskUserQuestion) are pinned by the footer phrase PAIR on ONE line
# (the dot never crosses newlines), which agent output cannot reproduce in
# the input-box region.
ASK_RE_PERMISSION = re.compile(r"Do you want to allow", re.IGNORECASE)
ASK_RE_SELECTION = re.compile(r"Enter to select.*Esc to cancel")

#: CC's input box bottom border is a line of these (verified live, 014).
INPUT_BORDER = "─"


def ask_visible(text: str | None) -> bool:
    """True while the pane shows a permission or selection prompt."""
    if not text:
        return False
    return bool(ASK_RE_PERMISSION.search(text)
                or ASK_RE_SELECTION.search(text))


def input_box_region_from_lines(lines: list[str]) -> list[str] | None:
    """The input-box/dialog region: last ``❯``-prefixed line through the
    next line consisting solely of the border glyph (None when no ``❯``).

    Scrollback is never part of the region, so a quoted footer above the
    box cannot strand ASK; while a selection prompt is open the option
    cursor is the ``❯`` line and the footer sits inside the region.
    """
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("❯"):
            start = i
    if start is None:
        return None
    region = []
    for line in lines[start:]:
        region.append(line)
        stripped = line.strip()
        if stripped and set(stripped) == {INPUT_BORDER}:
            break
    return region


def osc6_fill(rgb: tuple[int, int, int]) -> bytes:
    """One write carrying the three OSC 6 background-channel fills.

    ``ESC ] 6 ; 1 ; bg ; <chan> ; brightness ; N BEL`` for red, green and
    blue in order (byte order pinned by tests; spike-proven format).
    """
    red, green, blue = rgb
    out = bytearray()
    for chan, val in (("red", red), ("green", green), ("blue", blue)):
        out += (b"\x1b]6;1;bg;" + chan.encode("ascii")
                + b";brightness;" + str(int(val)).encode("ascii") + b"\x07")
    return bytes(out)


def blink_gate(text: str | None) -> bool:
    """True when the per-window progress text carries a listed gate marker."""
    if not text:
        return False
    return any(marker in text for marker in GATE_MARKERS)


def _tmux(runner, bin_: str, argv_tail: list, timeout: float = 2.0):
    """Run one tmux command via ``runner``; stripped stdout or None on failure."""
    try:
        proc = runner([bin_, *argv_tail], capture_output=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def list_client_ttys(runner, bin_: str, target: str) -> list[str]:
    """ttys of the clients attached to ``target`` ([] when none/unreachable).

    tmux prints the literal ``not a tty`` for clients without a tty; those
    lines (and blanks) are dropped. One tty per line, never whitespace
    split inside a path.
    """
    out = _tmux(runner, bin_, ["list-clients", "-t", target,
                               "-F", "#{client_tty}"])
    if out is None:
        return []
    return [line.strip() for line in out.splitlines()
            if line.strip() and line.strip() != "not a tty"]


def get_window_name(runner, bin_: str, target: str) -> str | None:
    """Current window name for ``target``, None when unreadable."""
    return _tmux(runner, bin_, ["display-message", "-p", "-t", target,
                                "#{window_name}"])


def rename_window(runner, bin_: str, target: str, name: str) -> bool:
    """Rename the window at ``target``; False on any tmux failure."""
    return _tmux(runner, bin_, ["rename-window", "-t", target, name]) is not None


def capture_pane(runner, bin_: str, target: str | None = None) -> str | None:
    """Visible pane text via ``capture-pane -p``; None on any failure.

    No target captures the pane this process lives in (``$TMUX_PANE``
    default); a target captures a sibling (the animator self-poll and
    the session-resident poller read the CC pane this way). Feed the
    result to ``ask_visible`` only through ``input_box_region_from_lines``
    so scrollback can never false-positive.
    """
    argv = ["capture-pane", "-p"]
    if target is not None:
        argv += ["-t", target]
    return _tmux(runner, bin_, argv)


def write_tty(path: str, data: bytes, *, _open=open) -> bool:
    """Write raw bytes to a client tty; False (never raise) on OSError.

    tmux never sees these bytes: the OSC sequences bypass the pane and go
    straight to the terminal emulator on the client's own tty.
    """
    try:
        fh = _open(path, "wb")
    except OSError:
        return False
    try:
        fh.write(data)
        return True
    except OSError:
        return False
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _debug_line(msg: str) -> None:
    """Append one diagnostic line to the blink log (never raises).

    The package has no stdlib logging module; the rotator/watchdogd
    file-line pattern is its convention, and the pane stdout is occupied
    by animation frames. One file keeps a non-blinking tab diagnosable.
    """
    try:
        log_path = paths.logs_dir() / "blink.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class Blinker:
    """Waiting-blink state machine driven by the animator's frame tick.

    ``on_text(text, now)`` arrives every animator frame with the current
    progress text and clock; ``on_exit()`` runs when the animator
    returns. The clock rides in on the call so tests fake it per tick.
    Every tmux/tty path swallows failures: the blink is decoration and
    must never break the progress animation.
    """

    def __init__(self, window_id: str, cfg: dict, *, store,
                 runner=subprocess.run, bin_: str = "tmux", tty_open=open,
                 log=None):
        self.window_id = window_id
        self._store = store
        self._runner = runner
        self._bin_ = bin_
        self._tty_open = tty_open
        self._log = log if log is not None else _debug_line
        self.enabled = config.blink_enabled(cfg)
        self._interval = config.blink_interval(cfg)
        self._fill = osc6_fill(
            RED_FILL if config.blink_no_color(cfg) else BLACK_FILL)
        self._blinking = False
        self._phase_own = True    # True: own color + original name showing
        self._last_toggle = 0.0
        self._original = None
        self._ttys: list[str] = []
        self._restored_logged = False
        # the sweep is deliberately NOT gated on enabled: a blink stranded
        # by a crash must heal even if blink was turned off since. With an
        # empty store it is a zero-call no-op, so "disabled restores
        # today's behavior exactly" still holds.
        self._startup_sweep()

    # -- internals ------------------------------------------------------

    def _startup_sweep(self) -> None:
        """Self-heal a blink stranded by a crashed animator (once per start)."""
        try:
            stored = self._store("blink_name")
            if not stored:
                return
            current = get_window_name(self._runner, self._bin_, self.window_id)
            if current and current.startswith("WAIT"):
                restored = rename_window(self._runner, self._bin_,
                                         self.window_id, stored)
                for tty in list_client_ttys(self._runner, self._bin_,
                                            self.window_id):
                    write_tty(tty, OSC6_RESET, _open=self._tty_open)
                if restored:
                    # keep the stored name on failure: the next animator
                    # start sweeps again
                    self._store("blink_name", None)
                    self._log(f"blink sweep window={self.window_id} "
                              f"restored stranded '{current}'")
        except Exception:
            pass

    def _start(self, now: float) -> None:
        name = get_window_name(self._runner, self._bin_, self.window_id)
        if name is None:
            self._log(f"blink skip window={self.window_id} "
                      f"reason=window name unreadable")
            return
        self._original = name
        self._store("blink_name", name)
        # Resolved per blink START (never cached across blinks): clients
        # attach and detach between waits.
        self._ttys = list_client_ttys(self._runner, self._bin_,
                                      self.window_id)
        self._blinking = True
        self._phase_own = True
        self._last_toggle = now
        self._restored_logged = False
        if self._ttys:
            self._log(f"blink start window={self.window_id} "
                      f"ttys={len(self._ttys)}")
        else:
            self._log(f"blink skip window={self.window_id} "
                      f"reason=no attached clients")

    def _wait_name(self, count: int) -> str:
        return "WAIT" if count <= 1 else f"WAIT[{count}]"

    def _apply(self, count: int) -> None:
        if self._phase_own:
            rename_window(self._runner, self._bin_, self.window_id,
                          self._original)
            for tty in self._ttys:
                write_tty(tty, OSC6_RESET, _open=self._tty_open)
        else:
            rename_window(self._runner, self._bin_, self.window_id,
                          self._wait_name(count))
            for tty in self._ttys:
                write_tty(tty, self._fill, _open=self._tty_open)

    def _restore(self) -> None:
        if self._original is not None:
            restored = rename_window(self._runner, self._bin_,
                                     self.window_id, self._original)
            for tty in self._ttys:
                write_tty(tty, OSC6_RESET, _open=self._tty_open)
            if restored:
                # Ownership released: a window rename the user makes
                # afterwards must survive animator exit. A FAILED rename
                # keeps both the name and the stored value: the store is
                # the only handle the startup sweep needs to heal the
                # stranded WAIT later.
                self._store("blink_name", None)
                self._original = None
                if not self._restored_logged:
                    self._log(f"blink restore window={self.window_id}")
                    self._restored_logged = True
        self._blinking = False

    # -- animator surface -------------------------------------------------

    def on_text(self, text: str | None, now: float,
                pane_text: str | None = None) -> None:
        """One animator tick: gate check, phase flip on the interval.

        ``pane_text`` is the caller's optional raw pane capture (the
        sibling pane in the animator, the CC pane in the watcher): a live
        selection prompt counts as a gate source even when the progress
        data file carries no marker: the status line is not invoked while
        CC waits on a question, so the file would stay stale (live bug
        2026-11-07).
        """
        if not self.enabled:
            return
        try:
            count = sum((text or "").count(m) for m in GATE_MARKERS)
            if pane_text is not None and ask_visible(pane_text):
                count += 1
            gated = count > 0
            if gated and not self._blinking:
                self._start(now)
                return
            if not self._blinking:
                return
            if not gated:
                self._restore()
                return
            if now - self._last_toggle >= self._interval:
                self._last_toggle = now
                self._phase_own = not self._phase_own
                self._apply(count)
        except Exception:
            pass

    def on_exit(self) -> None:
        """Animator is leaving: restore name and tab color (idempotent)."""
        if not self.enabled:
            return
        try:
            if self._blinking or self._original is not None:
                # covers the mid-blink exit AND the retry of a falling-edge
                # rename that tmux refused; _restore releases ownership on
                # success, so repeat calls are no-ops
                self._restore()
        except Exception:
            pass
