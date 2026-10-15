"""Progress pane (Phase 12, mode 1) — a separate 1-line tmux pane at 20 fps.

The CC binary hard-caps the status line refresh at 1 s
(``Math.max(1, j) * 1000``, verified from the 2.1.209 binary), so a true
50 ms sweep is impossible inside the status line itself. Mode 1 instead
spawns a 1-line pane WE own; its animator (``run_animator``) ticks at 20 fps
in our own process, gliding a bright same-hue band over a full-width rainbow
at ``PANE_CELLS_PER_S`` cells/s.

The pane shows the SAME segment text the status line renders — in mode 1 the
status line writes that plain text to ``runtime/progress-data-<window_id>.json``
on every render, and the pane reloads it when the file's mtime changes. The
file is PER WINDOW: with one global file, every window's pane showed whatever
another window's status line had written last (two CC windows showed each
other's context %).

Multi-CC safety: the pane record is a dict keyed by tmux WINDOW id (the pane
dies with its CC, the window is the stable identity), so several CC windows
in one session never clobber each other. Self-heal: the pane dies on any I/O
failure; the next status line render (``ensure_progress_pane``) recreates it.
A closed window's pane dies with the window; its leftover record entry and
data file are swept on the next ensure (per window — other open windows are
never touched; a tmux that does not answer is left alone entirely).

Both the data file and the pane-record key are ALSO scoped per tmux SERVER
(the ``$TMUX`` server pid, ``_tmux_server_token``): window ids are
per-server, so two servers each with a window ``@0`` would otherwise
clobber the same file and the same record entry (frozen pane, and one
server's pane showing the other server's %).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from . import atomic, paths
from .statusline import _RAINBOW, _SHIMMER

#: Window ids are tmux-generated (``@12``); restrict what we splice into
#: filenames to word chars / ``@`` / ``-``.
_VALID_WINDOW_ID = re.compile(r"[\w@-]+")

#: Band speed for the pane (cells per second of wall time). The status line
#: uses 4.0 (tuned for its 1 s cap); at a real 20 fps surface that reads
#: slow, so the pane is faster — ~4 s for a full-width sweep at 80 cols.
PANE_CELLS_PER_S = 24.0

# Width-jump guard (flashing-dot hotfix): a terminal resize leaves
# transient degenerate width reads (live-captured 50 cols on a 90-col
# terminal, down to 1 -> the single "·" frame the user saw). Never paint
# a frame narrower than _WIDTH_GUARD, and skip a frame whose read width
# teleports more than _WIDTH_JUMP cols from the previous READ width
# (mid-resize wobble); the last good frame stays on screen.
_WIDTH_GUARD = 10
_WIDTH_JUMP = 3

_PROGRESS_DATA_PREFIX = "progress-data-"
_PANE_RECORD_NAME = "progress-pane.json"


# ------------------------------------------------------------------ _frame


def _frame(width: int, text: str, now: float) -> str:
    """One full-width progress line at wall clock ``now`` (PURE).

    ``text`` is left-justified; the remainder is padded with ``·``. Char
    ``i`` renders ``_RAINBOW[i % 7]``; the 3-cell band (chars within ±1 of
    the glimmer at ``int(now * PANE_CELLS_PER_S) % width``) renders
    ``_SHIMMER[i % 7]`` — the brighter same-hue variant (revision 9's axis).
    """
    if width <= 0:
        return ""
    glimmer = int(now * PANE_CELLS_PER_S) % width
    n = len(_RAINBOW)
    cells = []
    for i in range(width):
        ch = text[i] if i < len(text) else "·"
        sgr = _SHIMMER[i % n] if abs(i - glimmer) <= 1 else _RAINBOW[i % n]
        cells.append(sgr + ch)
    return "".join(cells)


# ---------------------------------------------------------- progress-data I/O


def _valid_window_id(window_id: str | None) -> bool:
    return bool(window_id) and bool(
        _VALID_WINDOW_ID.fullmatch(window_id))


def _tmux_server_token() -> str:
    """This process's tmux server id, tokenized for filenames/record keys.

    ``$TMUX`` is ``<socket>,<server_pid>,<session>``; the server pid is
    unique per server and read identically by the status line and the
    in-pane animator (same server). Window ids (``@N``) are per-server —
    two servers both have a window ``@0`` — so data files and pane-record
    keys must carry this token or the servers clobber each other (the
    frozen pane + one server showing the other's %). Non-tmux -> "" (no
    pane is created there anyway).
    """
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return ""
    parts = tmux.split(",")
    if len(parts) >= 2 and parts[1].isdigit():
        return f"s{parts[1]}"
    return ""


def _pane_record_key(window_id: str) -> str:
    """Server-scoped pane-record key (see ``_tmux_server_token``)."""
    return f"{_tmux_server_token()}{window_id}"


def _progress_data_path(window_id: str):
    # per-window AND per-server: two tmux servers each have a window "@0"
    # — the bare window id made them clobber one shared data file
    return paths.runtime_dir() / (
        f"{_PROGRESS_DATA_PREFIX}{_tmux_server_token()}{window_id}.json")


def _read_progress_text(window_id: str | None) -> str:
    """This window's plain segment text, or the unknown placeholder.

    Mode 1's status line writes ``{"text": <plain>, "ts": ...}`` each render
    to the PER-WINDOW file; a missing/corrupt file or an invalid window id
    yields the placeholder (never a crash).
    """
    if not _valid_window_id(window_id):
        return "◆ …"
    try:
        data = json.loads(
            _progress_data_path(window_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "◆ …"
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text else "◆ …"


def _progress_data_mtime(window_id: str | None) -> float | None:
    if not _valid_window_id(window_id):
        return None
    try:
        return _progress_data_path(window_id).stat().st_mtime
    except OSError:
        return None


def write_progress_data(window_id: str | None, text: str) -> None:
    """Persist THIS window's plain segment text for the pane's animator.

    The mode-1 status line calls this on every render; the animator reloads
    when the mtime changes. Invalid window id or any failure -> no-op (state
    must never break the status line).
    """
    if not _valid_window_id(window_id):
        return
    try:
        path = _progress_data_path(window_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_json(path, {"text": text, "ts": time.time()})
    except OSError:
        pass


def drop_progress_data(window_id: str | None) -> None:
    """Remove this window's data file (SessionEnd cleanup). Never raises."""
    if not _valid_window_id(window_id):
        return
    try:
        _progress_data_path(window_id).unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------ pane record I/O


def _cli_path() -> str | None:
    """Absolute path to ``cc-continuity`` (a tmux pane shell may lack
    ``~/.local/bin`` on PATH), or None when not found."""
    return shutil.which("cc-continuity")


def _pane_record_path():
    return paths.runtime_dir() / _PANE_RECORD_NAME


def _read_pane_record() -> dict:
    try:
        rec = json.loads(_pane_record_path().read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_pane_record(rec: dict) -> None:
    try:
        path = _pane_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_json(path, rec)
    except OSError:
        pass


# ------------------------------------------------------------- tmux plumbing


def _tmux_call(runner, bin_: str, argv_tail: list, timeout: float = 2.0):
    """Run one tmux command via ``runner``; stripped stdout or None on failure."""
    try:
        proc = runner([bin_, *argv_tail], capture_output=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _pane_height(runner, bin_: str, pid: str, win: str) -> int | None:
    """Pane height when pane ``pid`` still exists in window ``win``, else
    None. One display-message is both the alive check (id + window must
    match) and the height read — the alive branch uses the height to
    self-restore a pane the user's border drag stretched off its designed
    1 line (the 23-row pane of the Bug Z capture; folding it into the
    alive check adds zero extra IPC)."""
    out = _tmux_call(runner, bin_, ["display-message", "-p", "-t", pid,
                                    "#{pane_id} #{window_id} #{pane_height}"])
    if not out:
        return None
    parts = out.split()
    if len(parts) != 3 or parts[0] != pid or parts[1] != win:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _pid_alive(pid: str) -> bool:
    """True when a process with this pid exists (signal-0 probe)."""
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OSError):
        return False


def _live_window_set(runner, bin_: str) -> set | None:
    """This server's live window ids (all sessions), or None on failure."""
    out = _tmux_call(runner, bin_,
                     ["list-windows", "-a", "-F", "#{window_id}"])
    if not out:
        return None
    return set(out.split())


def _sweep_closed_windows(runner, bin_: str) -> None:
    """Drop the record entries + data files of windows that are gone.

    When a window is closed tmux kills its panes (the animator process
    dies with it) but the record entry and the progress-data file
    survive as leftovers. Swept on every ensure, strictly per window:
    a key is dropped only when it is PROVABLY dead — the window is not
    in the server's live window list, or the server pid no longer
    exists. A live window's entry/file is never touched, so closing
    one window cannot affect the other open ones. If tmux does not
    answer list-windows (the resize-stall case) there is NO sweep —
    never act on an incomplete picture. Never raises.
    """
    token = _tmux_server_token()
    if not token:
        return
    live = _live_window_set(runner, bin_)
    if live is None:
        return
    try:
        def stale(key: str) -> bool:
            pid_part, sep, rest = key.partition("@")
            if not sep or not rest:
                return False  # unparseable — never drop what we can't attribute
            win = sep + rest  # "@0" — the window id keeps its @
            if pid_part == token:
                return win not in live
            if not (pid_part.startswith("s") and pid_part[1:].isdigit()):
                return False
            return not _pid_alive(pid_part[1:])
        rec = _read_pane_record()
        changed = False
        for key in list(rec):
            if stale(key):
                rec.pop(key, None)
                changed = True
        if changed:
            _write_pane_record(rec)
        for path in paths.runtime_dir().glob(
                _PROGRESS_DATA_PREFIX + "*.json"):
            key = path.name[len(_PROGRESS_DATA_PREFIX):-len(".json")]
            if not stale(key):
                continue
            path.unlink(missing_ok=True)
    except OSError:
        pass


def resolve_window_id(runner=subprocess.run,
                      tmux=os.environ.get) -> str | None:
    """This process's tmux window id (from ``$TMUX_PANE``), or None.

    Used by the status line (per-window data file), by ``_ensure`` (record
    key + pane argv), and by the pane process itself when it was launched
    without an explicit window id.
    """
    pane = tmux("TMUX_PANE")
    if not pane:
        return None
    bin_ = tmux("CC_CONTINUITY_TMUX_BIN") or "tmux"
    return _tmux_call(runner, bin_, ["display-message", "-p", "-t", pane,
                                     "#{window_id}"])


def ensure_progress_pane(cfg: dict, *, runner=subprocess.run,
                         tmux=os.environ.get) -> dict:
    """Ensure a 1-line progress pane exists for this CC window.

    NEVER raises: any failure (no ``$TMUX_PANE``, tmux gone, no binary, bad
    output) returns a status dict with ``created=False``. Self-heal: a dead
    pane is recreated on the next status line render. The record is keyed by
    tmux WINDOW id so several CC windows don't clobber each other.
    """
    try:
        return _ensure(cfg, runner, tmux)
    except Exception:
        return {"created": False, "reason": "error"}


def _ensure(cfg, runner, tmux) -> dict:
    pane = tmux("TMUX_PANE")
    if not pane:
        return {"created": False, "reason": "no-tmux-pane"}
    bin_ = tmux("CC_CONTINUITY_TMUX_BIN") or "tmux"
    win = resolve_window_id(runner, tmux)
    if not win:
        return {"created": False, "reason": "no-window"}
    # a closed window leaves its record entry + data file behind (its
    # pane is already gone with the window); sweep them — per window,
    # other open windows are never touched
    _sweep_closed_windows(runner, bin_)
    # record key is per-SERVER + window: two servers each have a window
    # "@0" — the bare window id made each render "find" the OTHER server's
    # pane, see it dead locally, and re-split forever
    record = _read_pane_record()
    key = _pane_record_key(win)
    entry = record.get(key)
    if isinstance(entry, dict):
        pid = entry.get("pane_id")
        if pid:
            height = _pane_height(runner, bin_, pid, win)
            if height is not None:
                # a 1-line pane is the design (split -l 1); a border drag
                # can stretch it — self-restore before returning alive
                if height != 1:
                    _tmux_call(runner, bin_,
                               ["resize-pane", "-t", pid, "-y", "1"])
                return {"created": False, "reason": "alive",
                        "window_id": win}
    cli = _cli_path()
    if not cli:
        return {"created": False, "reason": "no-binary"}
    # the pane process gets its window id on the command line: its data
    # file is per-window (one global file made windows share each
    # other's segment text)
    out = _tmux_call(runner, bin_,
                     ["split-window", "-d", "-l", "1", "-t", pane,
                      "-P", "-F", "#{pane_id} #{window_id}",
                      f"{cli} progress-pane {win}"])
    if not out:
        return {"created": False, "reason": "split-failed"}
    parts = out.split()
    if not parts:
        return {"created": False, "reason": "bad-output"}
    new_pid = parts[0]
    record[key] = {"pane_id": new_pid, "ts": time.time()}
    _write_pane_record(record)
    return {"created": True, "pane_id": new_pid, "window_id": win}


# --------------------------------------------------------------- run_animator


def run_animator(window_id: str | None = None, *, _frames=None,
                 _write=None, _now=None, _size=None, _text=None,
                 _mtime=None, _sleep=None) -> None:
    """Animate the progress line at ~20 fps until any I/O fails.

    Each frame is ``\\x1b[H\\x1b[2K`` + a full-width ``_frame``
    + ``\\x1b[0m\\x1b[J`` written to stdout: home to row 1 col 1, clear the
    line, paint, reset, clear below. The frame is ABSOLUTELY positioned
    (Bug Z) — a resize transition can race the width read and wrap one
    frame, dropping the pty cursor to the next row; ``\\r`` only returns to
    col 0 of the CURRENT row and can never bring it back, so the upper
    rows would stay frozen. ``\\x1b[H`` homes every frame (immune to cursor
    drift) and ``\\x1b[J`` erases any wrapped tail within one 50 ms frame.

    The segment text reloads when this window's
    ``progress-data-<window_id>.json`` mtime changes (the mode-1 status line
    rewrites it each render). ``window_id`` normally comes from the command
    line (``cc-continuity progress-pane <window_id>``); None falls back to
    resolving ``$TMUX_PANE`` and, failing that, animates the placeholder.
    The sleep is drift-compensated (0.05 s target minus the frame's own
    cost). ANY I/O failure ends the loop (returns) — the pane dies and the
    status line self-heal recreates it. Frames with degenerate resize
    widths are skipped (width-jump guard: narrower than ``_WIDTH_GUARD``,
    or a read that teleports more than ``_WIDTH_JUMP`` cols from the
    previous read width) — a resize never paints a single-character
    fragment; the last good frame stays on screen. ``_frames`` bounds the
    loop (tests).
    """
    write = _write if _write is not None else (lambda b: os.write(1, b))
    now = _now if _now is not None else time.time
    size = (_size if _size is not None
            else (lambda: shutil.get_terminal_size((80, 1))))
    sleep = _sleep if _sleep is not None else time.sleep
    if not _valid_window_id(window_id):
        window_id = resolve_window_id()
    text_fn = (_text if _text is not None
               else (lambda: _read_progress_text(window_id)))
    mtime_fn = (_mtime if _mtime is not None
                else (lambda: _progress_data_mtime(window_id)))
    try:
        cur_text = text_fn()
        cur_mtime = mtime_fn()
        i = 0
        last_width = None  # previous frame's READ width (jump guard)
        while True:
            # per-frame: the terminal can be resized while the animator
            # runs — a one-shot width makes frames wider than the pane,
            # which wrap and leave the padding tail ("··") visible. One
            # TIOCGWINSZ ioctl/frame is negligible at 20 fps.
            width = size().columns
            # width-jump guard: never paint a degenerate resize read —
            # below the floor, or a teleport from the previous READ
            # width. last_width tracks reads (not paints), so a real
            # resize that settles (80 -> 40, 40) is painted on its first
            # stable frame; only the jump frame itself is skipped.
            skipped = (width < _WIDTH_GUARD
                       or (last_width is not None
                           and abs(width - last_width) > _WIDTH_JUMP))
            last_width = width
            mt = mtime_fn()
            if mt != cur_mtime:
                # A file that DISAPPEARED (mtime None) is not a new text:
                # reloading it would downgrade the pane to the placeholder,
                # which stays until the next status line render — and no
                # render comes while the model is down (the freeze left the
                # pane on "…" for 30 minutes). Keep the last real text; a
                # reappearing file (None -> new mtime) still reloads.
                if mt is not None:
                    cur_text = text_fn()
                cur_mtime = mt
            t = now()
            if not skipped:
                # \x1b[H homes to row 1 col 1 every frame: \r alone can't
                # recover a pty cursor that a resize-race wrap dropped to
                # a lower row (Bug Z — every such race froze one more
                # rainbow line). \x1b[J then erases any wrapped tail/stale
                # rows. A skipped frame writes nothing: the last good
                # frame simply stays on screen.
                write(("\x1b[H\x1b[2K" + _frame(width, cur_text, t)
                       + "\x1b[0m\x1b[J").encode("utf-8"))
            i += 1
            if _frames is not None and i >= _frames:
                return
            t2 = now()
            sleep(max(0.0, 0.05 - (t2 - t)))
    except Exception:
        return
