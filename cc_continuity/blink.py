"""Waiting-blink primitives (issue #11).

Thin, self-contained layer: the OSC 6 tab-color escape sequences (spike
proven on iTerm2 3.7.6 and Terminal.app), the gate markers the blink
reacts to, and the tmux/tty helpers the ``Blinker`` state machine uses.

Import direction rule: ``progress_pane`` and ``statusline`` import from
here; this module imports nothing from them (nor from any other package
module) so a blink failure can never drag the animator down.
"""

OSC6_RESET = b"\x1b]6;1;bg;*;default\x07"

RED_FILL = (235, 20, 20)
BLACK_FILL = (0, 0, 0)

# Single source of truth for the gate markers. statusline.py composes its
# suffixes from these constants (composing its own leading spaces) so the
# writer and the gate can never drift apart silently.
MARKER_BLOCKED = "⛔ BLOCKED"
MARKER_ASK = "⚠ ASK"
GATE_MARKERS = (MARKER_BLOCKED, MARKER_ASK)


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
