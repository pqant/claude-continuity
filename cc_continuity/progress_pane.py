"""Progress pane (Phase 12, mode 1) — a separate 1-line tmux pane at 20 fps.

The CC binary hard-caps the status line refresh at 1 s
(``Math.max(1, j) * 1000``, verified from the 2.1.209 binary), so a true
50 ms sweep is impossible inside the status line itself. Mode 1 instead
spawns a 1-line pane WE own; its animator (``run_animator``) ticks at 20 fps
in our own process, gliding a bright same-hue band over a full-width rainbow
at ``PANE_CELLS_PER_S`` cells/s.

The pane shows the SAME segment text the status line renders — in mode 1 the
status line writes that plain text to ``runtime/progress-data.json`` on every
render, and the pane reloads it when the file's mtime changes.

Multi-CC safety: the pane record is a dict keyed by tmux WINDOW id (the pane
dies with its CC, the window is the stable identity), so several CC windows
in one session never clobber each other. Self-heal: the pane dies on any I/O
failure; the next status line render (``ensure_progress_pane``) recreates it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from . import atomic, paths
from .statusline import _RAINBOW, _SHIMMER

#: Band speed for the pane (cells per second of wall time). The status line
#: uses 4.0 (tuned for its 1 s cap); at a real 20 fps surface that reads
#: slow, so the pane is faster — ~4 s for a full-width sweep at 80 cols.
PANE_CELLS_PER_S = 24.0

_PROGRESS_DATA_NAME = "progress-data.json"
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


def _progress_data_path():
    return paths.runtime_dir() / _PROGRESS_DATA_NAME


def _read_progress_text() -> str:
    """The plain segment text for the pane, or the unknown placeholder.

    Mode 1's status line writes ``{"text": <plain>, "ts": ...}`` each render;
    a missing or corrupt file yields the placeholder (never a crash).
    """
    try:
        data = json.loads(_progress_data_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "⟳ …"
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text else "⟳ …"


def _progress_data_mtime() -> float | None:
    try:
        return _progress_data_path().stat().st_mtime
    except OSError:
        return None


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


def _pane_alive(runner, bin_: str, pid: str, win: str) -> bool:
    """True when pane ``pid`` still exists and belongs to window ``win``."""
    out = _tmux_call(runner, bin_, ["display-message", "-p", "-t", pid,
                                    "#{pane_id} #{window_id}"])
    if not out:
        return False
    parts = out.split()
    return len(parts) >= 2 and parts[0] == pid and parts[1] == win


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
    win = _tmux_call(runner, bin_, ["display-message", "-p", "-t", pane,
                                    "#{window_id}"])
    if not win:
        return {"created": False, "reason": "no-window"}
    record = _read_pane_record()
    entry = record.get(win)
    if isinstance(entry, dict):
        pid = entry.get("pane_id")
        if pid and _pane_alive(runner, bin_, pid, win):
            return {"created": False, "reason": "alive"}
    cli = _cli_path()
    if not cli:
        return {"created": False, "reason": "no-binary"}
    out = _tmux_call(runner, bin_,
                     ["split-window", "-d", "-l", "1", "-t", pane,
                      "-P", "-F", "#{pane_id} #{window_id}",
                      f"{cli} progress-pane"])
    if not out:
        return {"created": False, "reason": "split-failed"}
    parts = out.split()
    if not parts:
        return {"created": False, "reason": "bad-output"}
    new_pid = parts[0]
    record[win] = {"pane_id": new_pid, "ts": time.time()}
    _write_pane_record(record)
    return {"created": True, "pane_id": new_pid, "window_id": win}


# --------------------------------------------------------------- run_animator


def run_animator(*, _frames=None, _write=None, _now=None, _size=None,
                 _text=None, _mtime=None, _sleep=None) -> None:
    """Animate the progress line at ~20 fps until any I/O fails.

    Each frame is ``\\r\\x1b[2K`` + a full-width ``_frame`` + ``\\x1b[0m``
    written to stdout. The segment text reloads when ``progress-data.json``'s
    mtime changes (the mode-1 status line rewrites it each render). The sleep
    is drift-compensated (0.05 s target minus the frame's own cost). ANY I/O
    failure ends the loop (returns) — the pane dies and the status line
    self-heal recreates it. ``_frames`` bounds the loop (tests).
    """
    write = _write if _write is not None else (lambda b: os.write(1, b))
    now = _now if _now is not None else time.time
    size = (_size if _size is not None
            else (lambda: shutil.get_terminal_size((80, 1))))
    sleep = _sleep if _sleep is not None else time.sleep
    text_fn = _text if _text is not None else _read_progress_text
    mtime_fn = _mtime if _mtime is not None else _progress_data_mtime
    try:
        width = size().columns
        cur_text = text_fn()
        cur_mtime = mtime_fn()
        i = 0
        while True:
            mt = mtime_fn()
            if mt != cur_mtime:
                cur_text = text_fn()
                cur_mtime = mt
            t = now()
            write(("\r\x1b[2K" + _frame(width, cur_text, t)
                   + "\x1b[0m").encode("utf-8"))
            i += 1
            if _frames is not None and i >= _frames:
                return
            t2 = now()
            sleep(max(0.0, 0.05 - (t2 - t)))
    except Exception:
        return
