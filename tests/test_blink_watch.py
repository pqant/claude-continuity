"""blink_watch.py: the mode-0 session-resident waiting-blink poller.

Live bug 2026-11-07: AskUserQuestion sat unanswered for 46 minutes in a
mode-0 session and the tab never blinked. The Blinker only ran inside
the mode-1 animator, and CC does not invoke the status line while it
waits on a question, so the data file stayed marker-free. The poller is
spawned from SessionStart (independent of Notification/Stop hooks) and
drives the blink from (a) the sibling pane's input-box region and (b)
the ledger's unconsumed needs_human reason.
"""

import subprocess

from cc_continuity import blink, blink_watch, paths, progress_pane


_DIALOG_REGION = ("❯ 1. (A) go\n"
                  "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
                  + "─" * 40)

# Full capture: the footer QUOTED in agent chat above the box (real
# scrollback trap from the live session) plus an empty input box.
_PANE_QUOTED = (
    "agent: press Enter to select · Esc to cancel when ready\n"
    "more chat above the box\n"
    "❯ \n"
    + "─" * 40 + "\n"
    "junk below\n"
)


class Runner:
    """tmux-command recorder with per-command canned stdout lists."""

    def __init__(self, by_cmd=None, outs=()):
        self.calls = []
        self._outs = list(outs)
        self._by_cmd = {k: list(v) for k, v in (by_cmd or {}).items()}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        cmd = argv[1] if len(argv) > 1 else ""
        buf = self._by_cmd.get(cmd)
        if buf:
            out = buf.pop(0)
        elif self._outs:
            out = self._outs.pop(0)
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    def renames(self):
        return [a[4] for a in self.calls if a[1] == "rename-window"]


class Tty:
    def __init__(self):
        self.data = b""

    def write(self, chunk):
        self.data += chunk

    def close(self):
        pass


def _tty_open(path, mode="wb", **kw):
    return Tty()


def _sleep_noop(seconds):
    pass


class _Probe:
    """Fake Blinker recording every poller tick."""

    def __init__(self, window_id, cfg, *, store, runner, bin_="tmux",
                 tty_open=open, log=None):
        self.window_id = window_id
        self.store = store
        self.ticks = []
        self.exits = 0
        self.rearms = 0

    def on_text(self, text, now, pane_text=None):
        self.ticks.append((text, pane_text))

    def on_exit(self):
        self.exits += 1

    def rearm_attention(self):
        self.rearms += 1


class _FakeBlinkMod:
    """blink-shaped module: recording Blinker, scripted captures."""

    MARKER_BLOCKED = blink.MARKER_BLOCKED
    MARKER_REPLY = blink.MARKER_REPLY

    def __init__(self, captures=()):
        self._captures = list(captures)
        self.made = []
        self.capture_targets = []

    def Blinker(self, *args, **kw):
        b = _Probe(*args, **kw)
        self.made.append(b)
        return b

    def capture_pane(self, runner, bin_, target=None):
        self.capture_targets.append(target)
        if self._captures:
            return self._captures.pop(0)
        return None

    def input_box_region_from_lines(self, lines):
        return blink.input_box_region_from_lines(lines)


# ---------------------------------------------------------------- watch


def test_watch_feeds_region_and_keeps_quoted_footer_out(cc_home):
    fake = _FakeBlinkMod(captures=[_DIALOG_REGION, _PANE_QUOTED])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=2, _state=lambda d: "ACTIVE")
    b = fake.made[0]
    assert b.window_id == "@7"
    assert fake.capture_targets == ["%7", "%7"]
    assert _DIALOG_REGION in [b.ticks[0][1]]
    # second capture: the quoted footer sits ABOVE the box (region
    # starts at the last ❯ line) and must be filtered out
    assert b.ticks[1][1] == "❯ \n" + "─" * 40
    assert b.exits == 1


def test_watch_or_ledger_blocked_reason(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: "needs human for the deploy")
    fake = _FakeBlinkMod(captures=[None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=1, _state=lambda d: "ACTIVE")
    text, pane_text = fake.made[0].ticks[0]
    assert blink.MARKER_BLOCKED in text
    assert pane_text is None


def test_watch_unblocked_ledger_yields_empty_text(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: None)
    fake = _FakeBlinkMod(captures=[None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=1, _state=lambda d: "ACTIVE")
    assert fake.made[0].ticks[0][0] == ""


def test_watch_exits_when_state_leaves_live_set(cc_home):
    states = iter(["ACTIVE", "BOOTSTRAPPING", "ROTATION_PENDING"])
    fake = _FakeBlinkMod(captures=[None, None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=99,
        _state=lambda d: next(states))
    b = fake.made[0]
    assert len(b.ticks) == 2      # rotation handed over → quit
    assert b.exits == 1


def test_watch_exits_after_consecutive_capture_failures(cc_home):
    fake = _FakeBlinkMod(captures=[None, None, None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=99, _consec_fails=3,
        _state=lambda d: "ACTIVE")
    assert len(fake.capture_targets) == 3    # dead pane → quit, no spin
    assert fake.made[0].exits == 1


def test_watch_fail_counter_resets_on_successful_capture(cc_home):
    # fails below the cap, with a success between: must NOT quit.
    fake = _FakeBlinkMod(captures=[None, None, _DIALOG_REGION, None, None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=5, _consec_fails=3,
        _state=lambda d: "ACTIVE")
    assert len(fake.made[0].ticks) == 5


def test_watch_lifetime_cap(cc_home):
    # 12 h cap: clock advances one hour per call (start + one per tick).
    fake = _FakeBlinkMod(captures=[_DIALOG_REGION] * 30)
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open,
        _now=iter([k * 3600.0 for k in range(40)]).__next__,
        _sleep=_sleep_noop, _ticks=99, _state=lambda d: "ACTIVE")
    # one clock call per tick: ticks stamp 1 h .. 11 h, quit at 12 h
    assert len(fake.made[0].ticks) == 11
    assert fake.made[0].exits == 1


def test_watch_store_is_window_namespaced(cc_home):
    fake = _FakeBlinkMod(captures=[None])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: 0.0,
        _sleep=_sleep_noop, _ticks=1, _state=lambda d: "ACTIVE")
    store = fake.made[0].store
    store("blink_name", "orig")
    assert store("blink_name") == "orig"
    rec = progress_pane._read_pane_record()
    assert rec.get("blink-@7-blink_name") == "orig"


def test_watch_real_blinker_blinks_on_region_without_markers(cc_home):
    # end-to-end: real Blinker, data file stays clean (no _text seam at
    # all); the pane capture alone renames the window to WAIT.
    runner = Runner(by_cmd={"display-message": ["orig"],
                            "list-clients": ["/dev/ttys001"],
                            "capture-pane": [_DIALOG_REGION] * 10})
    clock = iter([k * 0.3 for k in range(50)])
    blink_watch.watch(
        "/sess", pane_id="%7", window_id="@7", cfg={}, _blink=blink,
        _runner=runner, _bin_="tmux", _tty_open=_tty_open,
        _now=lambda: next(clock), _sleep=_sleep_noop, _ticks=4,
        _state=lambda d: "ACTIVE")
    assert "WAIT" in runner.renames()


# ---------------------------------------------------------------- spawn


class Popen:
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.kw = kw
        self.pid = 5151


def _session_dir(tmp_path):
    return str(tmp_path / "session")


def test_spawn_skips_without_tmux_env(cc_home, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    pops = []
    assert blink_watch.spawn("/sess", _runner=Runner(), _popen=Popen,
                             _kill=lambda p, s: pops.append((p, s)),
                             _alive=lambda p: True) is None
    assert pops == []


def test_spawn_skips_without_pane_env(cc_home, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert blink_watch.spawn("/sess", _runner=Runner(), _popen=Popen,
                             _kill=lambda p, s: None,
                             _alive=lambda p: True) is None


def test_spawn_skips_mode1(cc_home, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(blink_watch.config, "load_config",
                        lambda: {"progress_type": 1})
    assert blink_watch.spawn("/sess", _runner=Runner(), _popen=Popen,
                             _kill=lambda p, s: None,
                             _alive=lambda p: True) is None


def test_spawn_skips_when_blink_disabled(cc_home, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(blink_watch.config, "load_config",
                        lambda: {"blink": False})
    assert blink_watch.spawn("/sess", _runner=Runner(), _popen=Popen,
                             _kill=lambda p, s: None,
                             _alive=lambda p: True) is None


def test_spawn_detached_argv_and_pidfile(cc_home, monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    runner = Runner(by_cmd={"display-message": ["@7"]})
    pops = []
    pid = blink_watch.spawn(
        "/sess", _runner=runner, _popen=lambda cmd, **kw: (
            pops.append((cmd, kw)) or Popen(cmd, **kw)),
        _kill=lambda p, s: None, _alive=lambda p: False)
    assert pid == 5151
    cmd, kw = pops[0]
    assert cmd[-7:] == ["blink-watch", "--pane", "%7", "--window", "@7",
                        "--session-dir", "/sess"]
    assert kw["start_new_session"] is True
    pidfile = paths.runtime_dir() / "blink-watch-@7.pid"
    assert pidfile.read_text(encoding="utf-8").strip() == "pid 5151"


def test_spawn_kills_alive_singleton_then_respawns(cc_home, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    runtime = paths.runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "blink-watch-@7.pid").write_text("pid 4242\n",
                                                encoding="utf-8")
    kills = []
    runner = Runner(by_cmd={"display-message": ["@7"]})
    pid = blink_watch.spawn("/sess", _runner=runner, _popen=Popen,
                            _kill=lambda p, s: kills.append((p, s)),
                            _alive=lambda p: p == 4242)
    import signal
    assert kills == [(4242, signal.SIGTERM)]
    assert pid == 5151
    assert (runtime / "blink-watch-@7.pid").read_text(
        encoding="utf-8").strip() == "pid 5151"


def test_spawn_never_raises_on_runner_failure(cc_home, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/srv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")

    def boom(argv, **kw):
        raise OSError("no tmux")
    assert blink_watch.spawn("/sess", _runner=boom, _popen=Popen,
                             _kill=lambda p, s: None,
                             _alive=lambda p: False) is None


# ---------------------------------------------------------------- reply blink
#
# Feature 2026-11-07: turn end (Stop hook writes reply_ready) blinks the
# tab until the user focuses it; the poller needs no status line render.


def _run_watch(fake, *, cfg=None, ticks=1, now=0.0, frontmost=None,
               clients=frozenset(), dwell=3, fade=600.0,
               frontmost_app=None, session_dir="/sess"):
    if frontmost_app is None:
        def app_fn():
            return None
    elif callable(frontmost_app):
        app_fn = frontmost_app
    else:
        seq = iter(frontmost_app)

        def app_fn():
            return next(seq, None)
    blink_watch.watch(
        session_dir, pane_id="%7", window_id="@7", cfg=cfg or {}, _blink=fake,
        _runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0,
                                                               b"", b""),
        _bin_="tmux", _tty_open=_tty_open, _now=lambda: now,
        _sleep=_sleep_noop, _ticks=ticks, _state=lambda d: "ACTIVE",
        _frontmost_tty=(frontmost if frontmost is not None else (lambda: None)),
        _client_ttys=clients if callable(clients) else (lambda: set(clients)),
        _dwell_ticks=dwell, _reply_fade=fade, _focus_log=None,
        _frontmost_app=app_fn)


def test_watch_reply_ready_blinks_without_block(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: 100.0)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake)
    text, _ = fake.made[0].ticks[0]
    assert blink.MARKER_REPLY in text
    assert blink.MARKER_BLOCKED not in text


def test_watch_reply_blocked_and_reply_both_gates(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: "deploy approval")
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: 100.0)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake)
    text, _ = fake.made[0].ticks[0]
    assert blink.MARKER_BLOCKED in text and blink.MARKER_REPLY in text


def test_watch_reply_disabled_by_config(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: 100.0)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, cfg={"blink_reply": False})
    text, _ = fake.made[0].ticks[0]
    assert text == ""


def test_watch_focus_dwell_consumes_reply(cc_home, monkeypatch):
    pending = [100.0]
    consumed = []
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending",
                        lambda d, **kw: pending[0])
    monkeypatch.setattr(blink_watch.ledger, "mark_reply_read",
                        lambda d: (consumed.append(1), pending.__setitem__(0, None)))
    fake = _FakeBlinkMod(captures=[None, None, None])
    _run_watch(fake, ticks=3, dwell=2,
               frontmost=lambda: "/dev/ttys001",
               clients=lambda: {"/dev/ttys001"})
    texts = [t for t, _ in fake.made[0].ticks]
    assert blink.MARKER_REPLY in texts[0]
    assert texts[1] == ""          # dwell reached: consumed, restore same tick
    assert texts[2] == ""          # no re-blink, no reply needed
    assert consumed == [1]


def test_watch_short_focus_continues_blink(cc_home, monkeypatch):
    pending = [100.0]
    consumed = []
    fronts = iter(["/dev/ttys001", None, "/dev/ttys001"])
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending",
                        lambda d, **kw: pending[0])
    monkeypatch.setattr(blink_watch.ledger, "mark_reply_read",
                        lambda d: consumed.append(1))
    fake = _FakeBlinkMod(captures=[None, None, None])
    _run_watch(fake, ticks=3, dwell=3,
               frontmost=lambda: next(fronts),
               clients=lambda: {"/dev/ttys001"})
    assert all(blink.MARKER_REPLY in t for t, _ in fake.made[0].ticks)
    assert consumed == []         # 1s glance: dwell reset, still waiting


def test_watch_reply_self_fades_without_focus_capability(cc_home, monkeypatch):
    # ts 0.0 vs clock 700.0 with a 600 s fade: never focusable (no
    # iTerm2), so the blink must not strand on an absent user.
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: 0.0)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, now=700.0, fade=600.0)
    text, _ = fake.made[0].ticks[0]
    assert text == ""


# ------------------------------------------------- focus dwell v3 re-arm
#
# User directive 2026-11-07: focus silences EVERY blink source.
# User directive 2026-11-08 (screen recordings 06:19/06:33, supersedes the
# focused half of the 11-07 rule): a new event (new needs-human reason, new
# dialog region, fresh reply ts) that lands while the tab is ALREADY focused
# is read on the spot and must NOT flash the foreground tab; the same event
# must still re-arm the blink while the user is not looking.

import pytest


@pytest.fixture(autouse=True)
def _no_real_focus_probe(monkeypatch):
    # Defense in depth: the suite must never exec osascript or tmux for
    # the focus comparison (seam defaults are None; this pins them).
    monkeypatch.setattr(blink_watch, "_default_frontmost_tty", lambda: None)
    monkeypatch.setattr(blink_watch, "_default_client_ttys",
                        lambda window_id, runner, bin_: set())


def test_watch_needs_human_new_reason_stays_silent_when_focused(cc_home, monkeypatch):
    reasons = iter(["deploy approval", "deploy approval", "merge conflict"])
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: next(reasons))
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: None)
    fake = _FakeBlinkMod(captures=[None, None, None])
    _run_watch(fake, ticks=3, dwell=2,
               frontmost=lambda: "/dev/ttys001",
               clients=lambda: {"/dev/ttys001"})
    b = fake.made[0]
    assert blink.MARKER_BLOCKED in b.ticks[0][0]    # first reason blinks once
    assert b.ticks[1] == ("", None)                 # dwell reached: silent
    assert b.ticks[2] == ("", None)                 # new reason while focused:
    #                                               # read on the spot, no flash


def test_watch_needs_human_new_reason_reblinks_when_not_focused(cc_home, monkeypatch):
    reasons = iter(["deploy approval", "deploy approval", "merge conflict"])
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: next(reasons))
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: None)
    fronts = iter(["/dev/ttys001", "/dev/ttys001", None])
    fake = _FakeBlinkMod(captures=[None, None, None])
    _run_watch(fake, ticks=3, dwell=2,
               frontmost=lambda: next(fronts),
               clients=lambda: {"/dev/ttys001"})
    b = fake.made[0]
    assert blink.MARKER_BLOCKED in b.ticks[0][0]    # first reason blinks
    assert b.ticks[1] == ("", None)                 # dwell reached: silent
    # user looked away and a new reason landed: it must call again
    assert blink.MARKER_BLOCKED in b.ticks[2][0]


def test_watch_ask_region_new_dialog_stays_silent_when_focused(cc_home, monkeypatch):
    region_b = ("❯ 1. (B) stay\n"
                "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
                + "─" * 40)
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: None)
    fake = _FakeBlinkMod(captures=[_DIALOG_REGION, _DIALOG_REGION,
                                   _DIALOG_REGION, region_b])
    _run_watch(fake, ticks=4, dwell=2,
               frontmost=lambda: "/dev/ttys001",
               clients=lambda: {"/dev/ttys001"})
    b = fake.made[0]
    assert b.ticks[0][1] == _DIALOG_REGION   # live dialog blinks while unread
    assert b.ticks[1] == ("", None)          # dwell reached: silent
    assert b.ticks[2] == ("", None)          # same region stays silent
    assert b.ticks[3] == ("", None)          # new dialog while focused:
    #                                         # read on the spot, no flash


def test_watch_ask_region_new_dialog_reblinks_when_not_focused(cc_home, monkeypatch):
    region_b = ("❯ 1. (B) stay\n"
                "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
                + "─" * 40)
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    monkeypatch.setattr(blink_watch.ledger, "reply_pending", lambda d, **kw: None)
    # one probe per tick EXCEPT the store-hit tick (the stored read
    # short-circuits before the probe): 3 calls for 4 ticks
    fronts = iter(["/dev/ttys001", "/dev/ttys001", None])
    fake = _FakeBlinkMod(captures=[_DIALOG_REGION, _DIALOG_REGION,
                                   _DIALOG_REGION, region_b])
    _run_watch(fake, ticks=4, dwell=2,
               frontmost=lambda: next(fronts),
               clients=lambda: {"/dev/ttys001"})
    b = fake.made[0]
    assert b.ticks[0][1] == _DIALOG_REGION   # live dialog blinks while unread
    assert b.ticks[1] == ("", None)          # dwell reached: silent
    assert b.ticks[2] == ("", None)          # same region stays silent
    assert b.ticks[3][1] == region_b         # new dialog after look-away: re-arms


# ------------------------------------------------------------ reply settle
#
# User ruling 2026-11-07 (supersedes the same-day background no-blink
# rule): an answer at an idle prompt must blink even with a background
# shell running - but only after a settle window (default 15 s). A
# session that wakes itself within the window (task notification, cron,
# late tool activity) was never waiting for the user and never blinks.


def test_watch_reply_settle_holds_blink_back(cc_home, tmp_path, monkeypatch):
    from cc_continuity import ledger
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    ledger.append(tmp_path, {"type": "reply_ready", "ts": 100.0})
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, now=110.0, cfg={"blink_reply_settle": 15},
               session_dir=tmp_path)
    text, _ = fake.made[0].ticks[0]
    assert text == ""              # 10 s old: still settling, stay silent


def test_watch_reply_blinks_after_settle(cc_home, tmp_path, monkeypatch):
    from cc_continuity import ledger
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    ledger.append(tmp_path, {"type": "reply_ready", "ts": 100.0})
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, now=116.0, cfg={"blink_reply_settle": 15},
               session_dir=tmp_path)
    text, _ = fake.made[0].ticks[0]
    assert blink.MARKER_REPLY in text


def test_watch_reply_cancelled_by_auto_continue(cc_home, tmp_path,
                                                monkeypatch):
    # The background wake landed (tool activity after the reply): the
    # session resumed itself, so even long past the settle window the
    # tab must stay silent.
    from cc_continuity import ledger
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason", lambda d: None)
    ledger.append(tmp_path, {"type": "reply_ready", "ts": 100.0})
    ledger.append(tmp_path, {"type": "tool_use", "tool": "Bash", "ts": 102.0})
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, now=300.0, cfg={"blink_reply_settle": 15},
               session_dir=tmp_path)
    text, _ = fake.made[0].ticks[0]
    assert text == ""


# ------------------------------------------------- app-background re-bounce
#
# Issue #61: macOS swallows a Dock bounce written while the terminal app
# is frontmost, so the poller re-fires a still-armed bounce when the app
# LEAVES the foreground. Transition edge only: no per-tick spam while
# the user stays away.


def test_watch_app_background_edge_refires_bounce(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: "deploy approval")
    monkeypatch.setattr(blink_watch.ledger, "reply_pending",
                        lambda d, **kw: None)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, ticks=2, frontmost_app=["iTerm2", "Finder"])
    # tick 1: still in iTerm2 (no edge); tick 2: Finder takes over: the
    # armed bounce is re-written exactly once.
    assert fake.made[0].rearms == 1


def test_watch_app_background_steady_never_refires(cc_home, monkeypatch):
    monkeypatch.setattr(blink_watch.ledger, "blocked_reason",
                        lambda d: "deploy approval")
    monkeypatch.setattr(blink_watch.ledger, "reply_pending",
                        lambda d, **kw: None)
    fake = _FakeBlinkMod(captures=[None])
    _run_watch(fake, ticks=3,
               frontmost_app=["Finder", "Finder", "Finder"])
    # no OBSERVED transition (the very first reading is already
    # background): nothing is re-written - and holding in the background
    # never spams either.
    assert fake.made[0].rearms == 0
