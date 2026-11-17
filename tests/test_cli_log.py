"""cli `log [blink|errors]` (issue #35): read-only tail, UTC stamps in local time.

errors.log carries explicit-offset UTC stamps (``[ISO+00:00]``); blink.log
stamps are naive LOCAL (``time.strftime``). Only offset-bearing stamps get
converted - converting naive ones would double-shift; untimestamped lines
(tracebacks) pass through untouched. Newest last, default tail 20,
``--follow`` streams appended lines. No session or rotation-state
dependency: the logs live at the root ``logs/`` dir.
"""

from datetime import timedelta, timezone

import pytest

from cc_continuity import cli, paths

TZ = timezone(timedelta(hours=1))

UTC_STAMPED = "[2026-11-17T07:28:07.123456+00:00] notify: push failed: 403"
NAIVE = "2026-11-17T11:51:06 focus suppress-stored @0 key='(False, False)'"
TRACEBACK = '  File "cc_continuity/hooks/common.py", line 58, in log_error'


def _write_log(name, lines):
    d = paths.logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("".join(line + "\n" for line in lines),
                          encoding="utf-8")


def _plain(count):
    return [f"evt {i}" for i in range(1, count + 1)]


# ------------------------------------------------------------- renderer

def test_render_converts_offset_stamps_only():
    out = cli.render_log_line(UTC_STAMPED, tz=TZ)
    assert out == "[2026-11-17T08:28:07.123456+01:00] notify: push failed: 403"


def test_render_keeps_naive_local_stamps_untouched():
    assert cli.render_log_line(NAIVE, tz=TZ) == NAIVE


def test_render_passes_untimestamped_lines():
    assert cli.render_log_line(TRACEBACK, tz=TZ) == TRACEBACK


def test_render_accepts_zulu_without_brackets():
    out = cli.render_log_line("2026-11-17T07:28:07Z heartbeat ok", tz=TZ)
    assert out.startswith("2026-11-17T08:28:07+01:00")
    assert out.endswith("heartbeat ok")


# ------------------------------------------------------------------ tail

def test_log_errors_tails_newest_last(cc_home, capsys):
    _write_log("errors.log", _plain(25))
    assert cli.main(["log", "errors", "--lines", "5"]) == 0
    assert capsys.readouterr().out.splitlines() == _plain(25)[-5:]


def test_log_defaults_to_twenty_and_nonpositive_falls_back(cc_home, capsys):
    _write_log("blink.log", _plain(25))
    assert cli.main(["log", "blink"]) == 0
    assert capsys.readouterr().out.splitlines() == _plain(25)[-20:]
    assert cli.main(["log", "blink", "--lines", "0"]) == 0
    assert capsys.readouterr().out.splitlines() == _plain(25)[-20:]


def test_log_missing_file_is_empty_success(cc_home, capsys):
    assert cli.main(["log", "errors"]) == 0
    assert capsys.readouterr().out == ""


def test_log_unknown_kind_is_rejected(cc_home):
    with pytest.raises(SystemExit) as exc:
        cli.main(["log", "rotator"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- follow

def test_follow_emits_appended_lines(cc_home):
    _write_log("blink.log", _plain(3))
    path = paths.logs_dir() / "blink.log"
    offset = len(path.read_bytes())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("evt 4\nevt 5\n")
    emitted = list(cli.follow_lines(path, offset, poll=0.01, max_lines=2))
    assert emitted == ["evt 4", "evt 5"]
