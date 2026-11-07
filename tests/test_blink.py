"""blink.py: OSC 6 tab-color primitives, gate markers, tmux/tty helpers."""

import subprocess

from cc_continuity import blink


def _runner_factory(outs):  # outs: list[str] per tmux call
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        out = (outs.pop(0) if outs else "").encode()
        return subprocess.CompletedProcess(argv, 0, out, b"")
    return runner, calls


def _failing_runner(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, b"", b"boom")


def test_osc6_bytes_exact():
    assert blink.OSC6_RESET == b"\x1b]6;1;bg;*;default\x07"
    assert blink.osc6_fill((0, 0, 0)) == (
        b"\x1b]6;1;bg;red;brightness;0\x07"
        b"\x1b]6;1;bg;green;brightness;0\x07"
        b"\x1b]6;1;bg;blue;brightness;0\x07")
    assert blink.osc6_fill(blink.RED_FILL) == (
        b"\x1b]6;1;bg;red;brightness;235\x07"
        b"\x1b]6;1;bg;green;brightness;20\x07"
        b"\x1b]6;1;bg;blue;brightness;20\x07")
    assert blink.RED_FILL == (235, 20, 20)
    assert blink.BLACK_FILL == (0, 0, 0)


def test_blink_gate_only_listed_markers():
    assert blink.blink_gate("◆ 61% Active Usage  ⛔ BLOCKED") is True
    assert blink.blink_gate("line one\nsecond  ⚠ ASK") is True
    assert blink.blink_gate("◆ 61% Active Usage") is False
    assert blink.blink_gate("") is False
    assert blink.blink_gate(None) is False


def test_gate_marker_constants_are_the_source_of_truth():
    assert blink.MARKER_BLOCKED == "⛔ BLOCKED"
    assert blink.MARKER_ASK == "⚠ ASK"
    assert blink.GATE_MARKERS == (blink.MARKER_BLOCKED, blink.MARKER_ASK)


def test_list_client_ttys_splits_lines_and_drops_not_a_tty():
    runner, calls = _runner_factory(["/dev/ttys025\n/dev/ttys002\n\nnot a tty\n"])
    assert blink.list_client_ttys(runner, "tmux", "@3") == ["/dev/ttys025",
                                                             "/dev/ttys002"]
    assert calls[0] == ["tmux", "list-clients", "-t", "@3",
                        "-F", "#{client_tty}"]
    assert blink.list_client_ttys(_failing_runner, "tmux", "@3") == []

    def raising(argv, **kw):
        raise OSError("no tmux binary")
    assert blink.list_client_ttys(raising, "tmux", "@3") == []


def test_get_window_name_strips_or_none():
    runner, calls = _runner_factory(["  claude-continuity-172  \n"])
    assert blink.get_window_name(runner, "tmux", "@3") == "claude-continuity-172"
    assert calls[0] == ["tmux", "display-message", "-p", "-t", "@3",
                        "#{window_name}"]
    assert blink.get_window_name(_failing_runner, "tmux", "@3") is None


def test_rename_window_argv_and_result():
    runner, calls = _runner_factory([""])
    assert blink.rename_window(runner, "tmux", "@3", "WAIT") is True
    assert calls[0] == ["tmux", "rename-window", "-t", "@3", "WAIT"]
    assert blink.rename_window(_failing_runner, "tmux", "@3", "WAIT") is False


def test_capture_pane_current_and_targeted():
    # no target: the pane this process lives in ($TMUX_PANE default);
    # with target: a sibling pane (the poller / animator capture path).
    runner, calls = _runner_factory(["a\nb\n", "c\n"])
    assert blink.capture_pane(runner, "tmux") == "a\nb"
    assert calls[0] == ["tmux", "capture-pane", "-p"]
    assert blink.capture_pane(runner, "tmux", target="@7") == "c"
    assert calls[1] == ["tmux", "capture-pane", "-p", "-t", "@7"]
    assert blink.capture_pane(_failing_runner, "tmux") is None


def test_write_tty_writes_bytes_and_swallows_oserror():
    class FakeFile:
        def __init__(self):
            self.data = b""
            self.closed = False
        def write(self, chunk):
            self.data += chunk
        def close(self):
            self.closed = True

    sink = FakeFile()
    opened = []
    def fake_open(path, mode="rb", **kw):
        opened.append((path, mode))
        return sink
    assert blink.write_tty("/dev/ttys025", b"\x1b]6;1;bg;", _open=fake_open) is True
    assert opened == [("/dev/ttys025", "wb")]
    assert sink.data == b"\x1b]6;1;bg;"
    assert sink.closed is True

    def raiser(path, mode="rb", **kw):
        raise OSError("device gone")
    assert blink.write_tty("/dev/ttys099", b"x", _open=raiser) is False


# ------------------------------------------------------------------ Blinker


class ScriptRunner:
    """Argv-capturing tmux runner replaying queued stdout lines."""

    def __init__(self, outs):
        self.outs = list(outs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = self.outs.pop(0) if self.outs else ""
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    def renames(self):
        return [c[4] for c in self.calls if "rename-window" in c]


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, chunk):
        self.data += chunk

    def close(self):
        pass


class FakeTTY:
    def __init__(self):
        self.files = {}

    def __call__(self, path, mode="rb", **kw):
        return self.files.setdefault(path, _Sink())

    def bytes_for(self, path):
        sink = self.files.get(path)
        return sink.data if sink else b""


def _store_dict():
    d = {}
    missing = object()

    def store(key, value=missing):
        if value is missing:
            return d.get(key)
        if value is None:
            d.pop(key, None)
        else:
            d[key] = value
        return d.get(key)
    return store


GATE = "◆ 61% ok  ⛔ BLOCKED"
NO_LOG = lambda msg: None  # noqa: E731  (tests never touch the real log)


def _blinker(r, tty, store=None, cfg=None):
    return blink.Blinker("@3", cfg or {}, store=store or _store_dict(),
                         runner=r, bin_="tmux", tty_open=tty, log=NO_LOG)


def test_blinker_disabled_touches_nothing():
    r, tty = ScriptRunner([]), FakeTTY()
    b = _blinker(r, tty, cfg={"blink": False})
    for t in (0.0, 0.5, 1.0):
        b.on_text(GATE, t)
    b.on_exit()
    assert r.calls == []
    assert tty.files == {}


def test_rising_edge_captures_and_stores_name():
    store, r, tty = _store_dict(), ScriptRunner(["demo-window", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, store=store)
    b.on_text(GATE, 0.0)
    assert r.calls[0] == ["tmux", "display-message", "-p", "-t", "@3",
                          "#{window_name}"]
    assert r.calls[1] == ["tmux", "list-clients", "-t", "@3",
                          "-F", "#{client_tty}"]
    assert store("blink_name") == "demo-window"
    assert r.renames() == []  # the flip itself waits for the first interval


def test_phase_flip_cadence():
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, cfg={"blink_interval": 0.5})
    # k/20.0: 0.05 s ticks whose 0.5 s boundaries land exactly (a 0.05
    # accumulator drifts below the >= comparison by tick 30).
    for k in range(31):
        b.on_text(GATE, k / 20.0)
    # t=0.5 goes dark (WAIT + black fill), t=1.0 back to own (orig + reset),
    # t=1.5 dark again: exactly the 0.5 s per-phase cadence.
    assert r.renames() == ["WAIT", "orig", "WAIT"]
    fill = blink.osc6_fill(blink.BLACK_FILL)
    assert tty.bytes_for("/dev/ttys001") == fill + blink.OSC6_RESET + fill


def test_no_color_uses_red_fill():
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, cfg={"blink_no_color": True})
    b.on_text(GATE, 0.0)
    b.on_text(GATE, 0.5)
    assert tty.bytes_for("/dev/ttys001") == blink.osc6_fill(blink.RED_FILL)


def test_multi_waiting_panes_count_in_name():
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty)
    text = "line one  ⛔ BLOCKED\nline two  ⛔ BLOCKED"
    b.on_text(text, 0.0)
    b.on_text(text, 0.5)
    assert r.renames() == ["WAIT[2]"]


def test_falling_edge_restores_once_to_seen_ttys():
    store = _store_dict()
    r, tty = ScriptRunner(["orig", "/dev/ttys001\n/dev/ttys002"]), FakeTTY()
    b = _blinker(r, tty, store=store)
    b.on_text(GATE, 0.0)
    b.on_text(GATE, 0.5)          # dark phase painted on both clients
    b.on_text("◆ 61% ok", 0.55)   # user answered: gate gone
    assert r.renames() == ["WAIT", "orig"]
    for path in ("/dev/ttys001", "/dev/ttys002"):
        assert tty.bytes_for(path) == (blink.osc6_fill(blink.BLACK_FILL)
                                       + blink.OSC6_RESET)
    assert store("blink_name") is None


def test_detached_zero_writes_renames_still_issued():
    r, tty = ScriptRunner(["orig", ""]), FakeTTY()
    b = _blinker(r, tty)
    b.on_text(GATE, 0.0)
    b.on_text(GATE, 0.5)
    assert r.renames() == ["WAIT"]   # harmless without a viewer
    assert tty.files == {}           # but not one byte to any tty


def test_startup_sweep_restores_stranded_wait_once():
    store = _store_dict()
    store("blink_name", "orig")
    # one queued stdout per runner call: display-message, rename-window,
    # list-clients.
    r, tty = ScriptRunner(["WAIT[2]", "", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, store=store)
    assert ["tmux", "rename-window", "-t", "@3", "orig"] in r.calls
    assert tty.bytes_for("/dev/ttys001") == blink.OSC6_RESET  # unconditional
    assert store("blink_name") is None
    n = len(r.calls)
    b.on_text("◆ ok", 1.0)
    assert len(r.calls) == n  # the sweep fired once, at construction


def test_startup_sweep_leaves_a_user_name_alone():
    store = _store_dict()
    store("blink_name", "orig")
    r, tty = ScriptRunner(["user-renamed"]), FakeTTY()
    _blinker(r, tty, store=store)
    assert r.renames() == []          # current name is not WAIT: hands off
    assert store("blink_name") == "orig"


def test_on_exit_restores_and_is_idempotent():
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty)
    b.on_text(GATE, 0.0)
    b.on_text(GATE, 0.5)
    b.on_exit()
    assert r.renames() == ["WAIT", "orig"]
    assert tty.bytes_for("/dev/ttys001").endswith(blink.OSC6_RESET)
    b.on_exit()  # idempotent second call must not raise or go wild


def test_on_exit_never_blinked_is_a_noop():
    r, tty = ScriptRunner([]), FakeTTY()
    b = _blinker(r, tty)
    b.on_exit()
    assert r.calls == []
    assert tty.files == {}


def test_blinker_never_raises_on_broken_plumbing():
    def raising(argv, **kw):
        raise OSError("tmux gone")
    b = blink.Blinker("@3", {}, store=_store_dict(), runner=raising,
                      bin_="tmux", tty_open=FakeTTY(), log=NO_LOG)
    b.on_text(GATE, 0.0)   # name unreadable: skip, no raise
    b.on_text(GATE, 0.5)
    b.on_exit()


def test_startup_sweep_runs_even_when_disabled():
    # spec: the stale self-heal is NOT conditioned on the blink config - a
    # stranded WAIT must heal even if the user turned blink off after the
    # crash. An empty store makes the sweep a zero-call no-op, so "disabled
    # restores today exactly" still holds.
    store = _store_dict()
    store("blink_name", "orig")
    r, tty = ScriptRunner(["WAIT[2]", "", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, store=store, cfg={"blink": False})
    assert ["tmux", "rename-window", "-t", "@3", "orig"] in r.calls
    assert tty.bytes_for("/dev/ttys001") == blink.OSC6_RESET
    assert store("blink_name") is None
    n = len(r.calls)
    b.on_text(GATE, 1.0)   # disabled: the state machine stays inert
    b.on_exit()
    assert len(r.calls) == n


def test_user_rename_after_clean_clear_survives_exit():
    # clean restore releases ownership: a rename the user made afterwards
    # must not be clobbered when the animator exits.
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty)
    b.on_text(GATE, 0.0)
    b.on_text(GATE, 0.5)
    b.on_text("◆ 61% ok", 0.55)   # gate gone, restore rename OK
    b.on_exit()
    assert r.renames() == ["WAIT", "orig"]


def test_failed_clear_keeps_stored_name_for_later_sweep():
    # a falling-edge rename that tmux refused must keep blink_name stored:
    # that is the ONLY handle the startup sweep needs to heal the WAIT.
    store = _store_dict()
    calls = []

    def runner(argv, **kw):
        calls.append(list(argv))
        if "rename-window" in argv and argv[4] == "orig":
            return subprocess.CompletedProcess(argv, 1, b"", b"tmux stall")
        out = b""
        if "display-message" in argv:
            out = b"orig"
        if "list-clients" in argv:
            out = b"/dev/ttys001"
        return subprocess.CompletedProcess(argv, 0, out, b"")

    b = blink.Blinker("@3", {}, store=store, runner=runner, bin_="tmux",
                      tty_open=FakeTTY(), log=NO_LOG)
    b.on_text(GATE, 0.0)
    assert store("blink_name") == "orig"
    b.on_text("◆ ok", 0.1)       # restore rename fails (tmux stall)
    assert store("blink_name") == "orig"


# ------------------------- AskUserQuestion / selection prompt (issue #11 follow-up)
#
# Live bug 2026-11-07 (connector-177): the AskUserQuestion dialog sat open
# for 46 minutes and the tab never blinked. Two independent gaps: the ASK
# regex matched ONLY the permission wording "Do you want to allow", and the
# status line (the sole marker writer) is never invoked while CC waits on a
# question. Detection must therefore also work from a raw pane capture:
# the selection footer is the stable phrase pair; question text and option
# labels are arbitrary. The fixture below is today's real dialog.

_BORDER = "─" * 40
_DIALOG_LINES = [
    "  Measurement job is closed, no re-roll.",
    "❯ 1. (A) competitor-reimplementation",
    "      Row labeled 'competitor-reimplementation' like P1.",
    "  2. (B) our-method-with-FSR-delta",
    "      Row labeled 'our method + FSR scoring delta'.",
    "  3. Type something.",
    "  4. Chat about this",
    "",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
    _BORDER,
]
_DIALOG = "\n".join(_DIALOG_LINES)
_PERMISSION_LINES = [
    "Do you want to allow this Bash command?",
    "❯ 1. Yes",
    "  2. No, and tell Claude what to do differently",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
    _BORDER,
]


def test_ask_visible_true_on_question_dialog():
    assert blink.ask_visible(_DIALOG) is True


def test_ask_visible_true_on_permission_prompt():
    assert blink.ask_visible("Do you want to allow `git push`?") is True
    assert blink.ask_visible("do you WANT to ALLOW editing this file?") is True


def test_ask_visible_false_on_ordinary_chat():
    assert blink.ask_visible("did you allow the file?") is False
    assert blink.ask_visible("yes please, go ahead") is False


def test_ask_visible_footer_requires_both_phrases_same_line():
    assert blink.ask_visible("Enter to select") is False
    assert blink.ask_visible("Esc to cancel") is False
    # one phrase per line does NOT match (the dot never crosses newlines)
    assert blink.ask_visible("Enter to select\nEsc to cancel") is False


def test_ask_visible_none_and_empty():
    assert blink.ask_visible(None) is False
    assert blink.ask_visible("") is False


def test_input_box_region_from_option_row_through_border():
    lines = ["scrollback above"] + _DIALOG_LINES + ["junk below"]
    region = blink.input_box_region_from_lines(lines)
    assert region is not None
    assert region[0] == "❯ 1. (A) competitor-reimplementation"
    assert "Enter to select · ↑/↓ to navigate · Esc to cancel" in region
    assert "junk below" not in region
    assert "scrollback above" not in region


def test_input_box_region_excludes_quoted_footer_above():
    # an agent quoting the footer in chat above the box (what happened in
    # this very session) must not strand an ASK: the region starts at the
    # LAST ❯ line.
    lines = ["Enter to select · ↑/↓ to navigate · Esc to cancel",
             "❯ ", _BORDER]
    region = blink.input_box_region_from_lines(lines)
    assert region == ["❯ ", _BORDER]
    assert blink.ask_visible("\n".join(region)) is False


def test_input_box_region_none_without_prompt_marker():
    assert blink.input_box_region_from_lines(["plain text", _BORDER]) is None


def test_blinker_starts_on_pane_dialog_without_data_marker():
    r = ScriptRunner(["conn-tab", "/dev/ttys001"])
    tty = FakeTTY()
    b = _blinker(r, tty, store=_store_dict())
    b.on_text("◆ 29% Active Usage", 0.0, pane_text=_DIALOG)
    b.on_text("◆ 29% Active Usage", 0.5, pane_text=_DIALOG)
    assert r.renames() == ["WAIT"]
    assert tty.bytes_for("/dev/ttys001") == blink.osc6_fill(blink.BLACK_FILL)


def test_blinker_pane_falling_edge_restores():
    store = _store_dict()
    r, tty = ScriptRunner(["conn-tab", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty, store=store)
    b.on_text("◆ 29% Active Usage", 0.0, pane_text=_DIALOG)
    b.on_text("◆ 29% Active Usage", 0.5, pane_text=_DIALOG)  # dark
    b.on_text("◆ 29% Active Usage", 0.6, pane_text="")       # answered
    assert r.renames() == ["WAIT", "conn-tab"]
    assert tty.bytes_for("/dev/ttys001") == (
        blink.osc6_fill(blink.BLACK_FILL) + blink.OSC6_RESET)
    assert store("blink_name") is None


def test_blinker_pane_ask_adds_to_wait_count():
    r, tty = ScriptRunner(["orig", "/dev/ttys001"]), FakeTTY()
    b = _blinker(r, tty)
    b.on_text(GATE, 0.0, pane_text=_DIALOG)   # 1 data marker + pane ask
    b.on_text(GATE, 0.5, pane_text=_DIALOG)
    assert r.renames() == ["WAIT[2]"]
