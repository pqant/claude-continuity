"""progress_pane.py: Phase 12 mode 1 — a separate 1-line tmux pane.

The statusline's 1 s refresh cap (CC binary, ``Math.max(1, j)``) makes a
true 50 ms sweep impossible in the status line itself. Mode 1 instead
spawns a 1-line pane we own, whose animator ticks at 20 fps in our own
process. The pane shows the same segment text the statusline renders
(mode 1 writes it to ``runtime/progress-data.json``), left-justified over
a full-width rainbow with a bright shimmer band gliding at 24 cells/s.

The pane is keyed by tmux WINDOW id (multi-CC safe: several CC windows in
one session must not clobber each other) and self-heals — if it dies, the
next statusline render recreates it.
"""

import json
import re
import time

import pytest

from cc_continuity import atomic, paths, progress_pane
from cc_continuity.statusline import _RAINBOW, _SHIMMER

_SGR_RE = re.compile(r"(\x1b\[38;2;\d+;\d+;\d+m)(.)")


def _cells(frame):
    """``[(sgr, char), ...]`` — one entry per visible cell."""
    return _SGR_RE.findall(frame)


def _visible(frame):
    """Strip the CR + clear-line prefix, the reset suffix, and all SGR codes.

    Handles both a raw ``_frame`` (SGR cells only) and an animator line
    (``\\r\\x1b[2K`` + cells + ``\\x1b[0m``) down to its visible characters.
    """
    s = re.sub(r"\x1b\[2K", "", frame)      # clear-line (no 'm' terminator)
    s = re.sub(r"\x1b\[[0-9;]*m", "", s)    # SGR color/reset codes
    return s.replace("\r", "")


# ------------------------------------------------------------------ _frame


def test_frame_fills_width_with_text_and_dots():
    frame = progress_pane._frame(10, "hi", 0.0)
    assert _visible(frame) == "hi" + "·" * 8
    assert len(_cells(frame)) == 10


def test_frame_truncates_text_longer_than_width():
    frame = progress_pane._frame(4, "abcdef", 0.0)
    assert _visible(frame) == "abcd"
    assert len(_cells(frame)) == 4


def test_frame_zero_width_is_empty():
    assert progress_pane._frame(0, "abc", 0.0) == ""
    assert progress_pane._frame(-3, "abc", 0.0) == ""


def test_frame_band_at_glimmer_exact_sgrs():
    # now=0.0 -> glimmer 0 -> band {0,1}: chars 0,1 shimmer, rest base
    cells = _cells(progress_pane._frame(8, "abcdef", 0.0))
    assert cells[0][0] == _SHIMMER[0]
    assert cells[1][0] == _SHIMMER[1]
    assert cells[2][0] == _RAINBOW[2]
    assert cells[3][0] == _RAINBOW[3]


def test_frame_band_advances_one_cell_per_tick():
    # now=0.05 -> int(0.05*24)=int(1.2)=1 -> band {0,1,2}
    cells = _cells(progress_pane._frame(8, "abcdef", 0.05))
    assert cells[0][0] == _SHIMMER[0]   # abs(0-1)<=1
    assert cells[1][0] == _SHIMMER[1]
    assert cells[2][0] == _SHIMMER[2]
    assert cells[3][0] == _RAINBOW[3]   # abs(3-1)=2, out of band


def test_frame_band_moves_over_time():
    assert progress_pane._frame(20, "x", 0.0) != \
        progress_pane._frame(20, "x", 0.05)


def test_frame_glimmer_wraps_at_width():
    # int(1.0*24)=24; 24 % 8 == 0 -> same band as now=0.0
    assert progress_pane._frame(8, "abcdef", 1.0) == \
        progress_pane._frame(8, "abcdef", 0.0)


def test_frame_speed_is_cells_per_second():
    # 24 cells/s: one full 24-wide sweep = 1.0 s
    assert progress_pane._frame(24, "", 0.0) == \
        progress_pane._frame(24, "", 1.0)
    # and 0.5 s is half a sweep -> glimmer 12
    a = progress_pane._frame(24, "", 0.5)
    assert _cells(a)[12][0] == _SHIMMER[12 % 7]


# -------------------------------------------------------- ensure_progress_pane

class _CP:
    """A minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _classify(argv):
    a = list(argv)
    if "split-window" in a:
        return "split"
    if "display-message" in a:
        fmt = a[-1]
        if fmt == "#{window_id}":
            return "window"
        if fmt == "#{pane_id} #{window_id}":
            return "pane"
    return "other"


def _fake_run(responses, calls):
    def run(argv, **kw):
        calls.append(list(argv))
        val = responses.get(_classify(argv))
        if isinstance(val, BaseException):
            raise val
        if val is None:
            return _CP(1, b"")
        if isinstance(val, str):
            val = val.encode()
        return _CP(0, val)
    return run


def _env(**vars):
    def get(key, default=None):
        return vars.get(key, default)
    return get


def test_ensure_no_tmux_pane_is_noop(cc_home, monkeypatch):
    calls = []
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    rec = progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({}, calls), tmux=_env())
    assert rec["created"] is False
    assert rec["reason"] == "no-tmux-pane"
    assert calls == []  # tmux never spawned


def test_ensure_alive_pane_is_noop(cc_home, monkeypatch):
    calls = []
    p = paths.runtime_dir() / "progress-pane.json"
    atomic.atomic_write_json(p, {"12": {"pane_id": "%50", "ts": 0}})
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    rec = progress_pane.ensure_progress_pane(
        {},
        runner=_fake_run({"window": "12", "pane": "%50 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    assert rec["created"] is False
    assert rec["reason"] == "alive"
    # the status line needs the window id to write its per-window data file
    assert rec["window_id"] == "12"
    assert not any("split-window" in c for c in calls)


def test_ensure_dead_pane_recreates(cc_home, monkeypatch):
    calls = []
    p = paths.runtime_dir() / "progress-pane.json"
    atomic.atomic_write_json(p, {"12": {"pane_id": "%50", "ts": 0}})
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    rec = progress_pane.ensure_progress_pane(
        {},
        runner=_fake_run({"window": "12", "pane": None,
                          "split": "%99 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    assert rec["created"] is True
    assert rec["pane_id"] == "%99"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["12"]["pane_id"] == "%99"
    splits = [c for c in calls if "split-window" in c]
    assert len(splits) == 1
    # -d (don't steal focus) and -l 1 (one line) are part of the contract
    assert "-d" in splits[0] and "-l" in splits[0] and "1" in splits[0]
    # the pane process must know its window (its data file is per-window)
    assert splits[0][-1] == "/fake/cc-continuity progress-pane 12"
    assert rec["window_id"] == "12"


def test_ensure_missing_record_creates(cc_home, monkeypatch):
    calls = []
    assert not (paths.runtime_dir() / "progress-pane.json").exists()
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    rec = progress_pane.ensure_progress_pane(
        {},
        runner=_fake_run({"window": "12", "split": "%99 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    assert rec["created"] is True
    # no "pane" alive-check was issued (nothing to check)
    assert not any(_classify(c) == "pane" for c in calls)


def test_ensure_record_keyed_by_window(cc_home, monkeypatch):
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    calls = []
    progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({"window": "12", "pane": None,
                              "split": "%99 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    calls.clear()
    # a second CC window must create its own pane, not clobber window 12
    progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({"window": "15", "pane": None,
                              "split": "%101 15"}, calls),
        tmux=_env(TMUX_PANE="%20"))
    on_disk = json.loads(
        (paths.runtime_dir() / "progress-pane.json").read_text(encoding="utf-8"))
    assert on_disk["12"]["pane_id"] == "%99"
    assert on_disk["15"]["pane_id"] == "%101"


def test_ensure_record_keyed_per_server(cc_home, monkeypatch):
    # the bug this fixes: window ids are per-server, so two tmux servers
    # each with a window "12" shared ONE record entry — each render found
    # the OTHER server's pane, thought it dead locally, and re-split forever
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")
    calls = []
    monkeypatch.setenv("TMUX", "/tmp/srv,1111,0")
    progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({"window": "12", "pane": None,
                              "split": "%99 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    calls.clear()
    monkeypatch.setenv("TMUX", "/tmp/srv,2222,0")
    progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({"window": "12", "pane": None,
                              "split": "%101 12"}, calls),
        tmux=_env(TMUX_PANE="%7"))
    on_disk = json.loads(
        (paths.runtime_dir() / "progress-pane.json").read_text(encoding="utf-8"))
    # distinct per-server keys, each keeping its own pane
    assert set(on_disk) == {"s111112", "s222212"}
    assert on_disk["s111112"]["pane_id"] == "%99"
    assert on_disk["s222212"]["pane_id"] == "%101"


def test_ensure_no_binary_is_noop(cc_home, monkeypatch):
    calls = []
    monkeypatch.setattr(progress_pane, "_cli_path", lambda: None)
    rec = progress_pane.ensure_progress_pane(
        {}, runner=_fake_run({"window": "12", "pane": None}, calls),
        tmux=_env(TMUX_PANE="%7"))
    assert rec["created"] is False
    assert rec["reason"] == "no-binary"
    assert not any("split-window" in c for c in calls)


def test_ensure_never_raises_on_runner_error(cc_home, monkeypatch):
    monkeypatch.setattr(progress_pane, "_cli_path",
                        lambda: "/fake/cc-continuity")

    def boom(argv, **kw):
        raise OSError("tmux gone")

    rec = progress_pane.ensure_progress_pane(
        {}, runner=boom, tmux=_env(TMUX_PANE="%7"))
    assert isinstance(rec, dict)
    assert rec["created"] is False


# --------------------------------------------------------------- run_animator


def _size_obj(columns):
    class _S:
        pass
    s = _S()
    s.columns = columns
    return s


def test_animator_writes_full_width_frames(cc_home):
    written = []
    progress_pane.run_animator(
        _frames=3, _write=written.append, _now=lambda: 0.0,
        _size=lambda: _size_obj(20), _text=lambda: "⟳ 9% ok",
        _mtime=lambda: 1, _sleep=lambda s: None)
    assert len(written) == 3
    assert written[0].startswith(b"\r\x1b[2K")
    assert written[0].endswith(b"\x1b[0m")
    assert len(_visible(written[0].decode("utf-8"))) == 20
    # the segment text is left-justified
    assert _visible(written[0].decode("utf-8")).startswith("⟳ 9% ok")


def test_animator_reloads_text_on_mtime_change(cc_home):
    written = []
    state = {"mtime": 1, "text": "AAAAA"}
    flips = {"n": 0}

    def sleep(s):
        flips["n"] += 1
        if flips["n"] == 2:  # after the 2nd frame
            state["mtime"] = 2
            state["text"] = "BBBBB"

    progress_pane.run_animator(
        _frames=4, _write=written.append, _now=lambda: 0.0,
        _size=lambda: _size_obj(5), _text=lambda: state["text"],
        _mtime=lambda: state["mtime"], _sleep=sleep)
    v = [ _visible(w.decode("utf-8")) for w in written]
    # frames 1-2 show the old text, 3-4 the reloaded text
    assert v[0].startswith("AAAAA") and v[1].startswith("AAAAA")
    assert v[2].startswith("BBBBB") and v[3].startswith("BBBBB")


def test_animator_keeps_last_text_when_file_disappears(cc_home):
    # The data file vanishing mid-run (e.g. cleanup while the model
    # server is down) must NOT downgrade the pane to the placeholder:
    # nothing rewrites the file until the next status line render, and
    # no render comes while the model is down — the placeholder would
    # be the only thing on the pane for the whole outage (the freeze
    # the user saw: a pane sitting on "…"). The file reappearing with
    # new content still reloads normally.
    written = []
    state = {"mtime": 1, "text": "AAAAA"}
    flips = {"n": 0}

    def text():
        # what _read_progress_text returns: the placeholder when the
        # file is missing/corrupt
        return state["text"] if state["mtime"] is not None else "⟳ …"

    def sleep(s):
        flips["n"] += 1
        if flips["n"] == 2:      # after frame 2: file disappears
            state["mtime"] = None
        elif flips["n"] == 4:    # after frame 4: file reappears
            state["mtime"] = 3
            state["text"] = "BBBBB"

    progress_pane.run_animator(
        _frames=6, _write=written.append, _now=lambda: 0.0,
        _size=lambda: _size_obj(5), _text=text,
        _mtime=lambda: state["mtime"], _sleep=sleep)
    v = [_visible(w.decode("utf-8")) for w in written]
    # while the file is gone: keep the last real text (no "⟳ …")
    assert v[0].startswith("AAAAA")
    assert v[2].startswith("AAAAA")
    assert v[3].startswith("AAAAA")
    # file back with new content -> reload
    assert v[4].startswith("BBBBB")
    assert v[5].startswith("BBBBB")


def test_animator_returns_on_io_failure(cc_home):
    # a closed pane (write raises) must END the loop, not raise — the
    # statusline self-heal recreates the pane on the next render.
    def boom(b):
        raise OSError("tty closed")

    progress_pane.run_animator(
        _frames=10, _write=boom, _now=lambda: 0.0,
        _size=lambda: _size_obj(10), _text=lambda: "x",
        _mtime=lambda: 1, _sleep=lambda s: None)
    # reaching this line == no exception propagated


def test_animator_drift_compensates_sleep(cc_home):
    # the sleep argument must be clamped to >= 0 and reflect elapsed frame
    # time (0.05 s target minus the frame's own cost, measured around the
    # write). Simulate a 20 ms frame -> sleep should be ~30 ms.
    sleeps = []
    t = {"v": 0.0}

    def now():
        return t["v"]

    def write(b):
        t["v"] += 0.02  # the render/write costs 20 ms of wall time

    progress_pane.run_animator(
        _frames=2, _write=write, _now=now,
        _size=lambda: _size_obj(5), _text=lambda: "x",
        _mtime=lambda: 1, _sleep=sleeps.append)
    assert len(sleeps) == 1  # no sleep after the final frame
    assert sleeps[0] == pytest.approx(0.03, abs=1e-6)


def test_read_progress_text_defaults_to_unknown(cc_home):
    # no data file yet -> the placeholder, never a crash
    assert progress_pane._read_progress_text("12") == "⟳ …"


def test_read_progress_text_reads_file(cc_home):
    p = paths.runtime_dir() / "progress-data-12.json"
    atomic.atomic_write_json(p, {"text": "⟳ 9% ok", "ts": time.time()})
    assert progress_pane._read_progress_text("12") == "⟳ 9% ok"
    # corrupt file -> placeholder
    p.write_text("{not json", encoding="utf-8")
    assert progress_pane._read_progress_text("12") == "⟳ …"


def test_read_progress_text_isolated_per_window(cc_home):
    # the bug this fixes: one global progress-data.json made every window's
    # pane show whatever ANOTHER window's status line wrote last
    atomic.atomic_write_json(
        paths.runtime_dir() / "progress-data-12.json",
        {"text": "⟳ 61% ok", "ts": time.time()})
    assert progress_pane._read_progress_text("12") == "⟳ 61% ok"
    assert progress_pane._read_progress_text("15") == "⟳ …"


def test_write_progress_data_roundtrip(cc_home):
    progress_pane.write_progress_data("12", "⟳ 61% ok")
    p = paths.runtime_dir() / "progress-data-12.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["text"] == "⟳ 61% ok"
    assert isinstance(data["ts"], float)
    assert progress_pane._read_progress_text("12") == "⟳ 61% ok"


def test_drop_progress_data(cc_home):
    progress_pane.write_progress_data("12", "⟳ 61% ok")
    progress_pane.drop_progress_data("12")
    assert not (paths.runtime_dir() / "progress-data-12.json").exists()
    # missing file is a no-op (never raises)
    progress_pane.drop_progress_data("15")


def test_progress_data_isolated_per_server(cc_home, monkeypatch):
    # the bug this fixes: two tmux servers each have a window "12", so the
    # shared progress-data-12.json made one server's pane show the OTHER
    # server's % (and churned the pane record)
    monkeypatch.setenv("TMUX", "/tmp/srv,1111,0")
    progress_pane.write_progress_data("12", "⟳ 61% ok")
    assert progress_pane._read_progress_text("12") == "⟳ 61% ok"
    # a different server reading the same window id sees its own (empty)
    # file, not server 1111's data
    monkeypatch.setenv("TMUX", "/tmp/srv,2222,0")
    assert progress_pane._read_progress_text("12") == "⟳ …"
