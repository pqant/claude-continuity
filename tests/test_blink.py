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
