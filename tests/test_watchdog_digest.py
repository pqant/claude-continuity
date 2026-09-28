"""watchdog_digest.py: Phase 13 digest relay (the user's cron request).

Pure decision layer: notable(), format_digest(), state roundtrip,
read_new_entries(), push_allowed(), pick_target(). I/O layer comes in
the next TDD batch (run_once, inject_pane, plist)."""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from cc_continuity import paths
from cc_continuity import watchdog_digest as wd


def _t(*args):
    """Local HH:MM:SS for a ts (same local-time basis the digest uses)."""
    return datetime.fromtimestamp(args[0]).strftime("%H:%M:%S")


# --- notable ------------------------------------------------------------------


def test_notable_down_health():
    assert wd.notable({"ts": 1.0, "health": "DOWN"}) is True


def test_notable_unknown_health():
    # health != UP: the cold-start UNKNOWN tick (one failed probe after a
    # daemon restart) is a potential early warning — digest it.
    assert wd.notable({"ts": 1.0, "health": "UNKNOWN"}) is True


def test_notable_up_no_events_no_recovery():
    assert wd.notable({"ts": 1.0, "health": "UP", "events": [],
                       "recovery": {"ran": False, "exit": None,
                                    "elapsed_s": 0.0}}) is False


def test_notable_up_with_dgx_up_event():
    assert wd.notable({"ts": 1.0, "health": "UP",
                       "events": ["dgx_up"]}) is True


def test_notable_up_with_power_cycle_failed_event():
    assert wd.notable({"ts": 1.0, "health": "UP",
                       "events": ["power_cycle_failed:plug unreachable"]}) \
        is True


def test_notable_up_recovery_ran():
    assert wd.notable({"ts": 1.0, "health": "UP",
                       "recovery": {"ran": True, "exit": 0,
                                    "elapsed_s": 15.2}}) is True


def test_notable_error_tick_without_health_key():
    # A tick exception line has no health key — it is notable (something
    # broke in the daemon itself).
    assert wd.notable({"ts": 1.0, "error": "tick exception"}) is True


# --- format_digest --------------------------------------------------------------


def test_format_digest_single_down_entry():
    ts = 1790000000.0
    now = ts + 120
    entry = {"ts": ts, "health": "DOWN",
             "recovery": {"ran": True, "exit": 0, "elapsed_s": 15.2},
             "events": ["dgx_down"]}
    out = wd.format_digest([entry], now)
    lines = out.splitlines()
    assert lines[0] == (
        f"[cc-continuity watchdog digest "
        f"{datetime.fromtimestamp(now).strftime('%H:%M')}]")
    assert lines[1] == (
        f"{_t(ts)} health=DOWN recovery(exit=0,15s) ev=[dgx_down]")
    # Phase 19: the digest is a LOG (display-popup), not a prompt — no
    # ack is expected
    assert lines[2] == "(cc-continuity watchdog digest — log only, not a prompt)"
    assert len(lines) == 3


def test_format_digest_omits_absent_recovery_and_events():
    ts = 1790000100.0
    entry = {"ts": ts, "health": "UP", "events": ["dgx_up"]}
    line = wd.format_digest([entry], ts + 60).splitlines()[1]
    assert line == f"{_t(ts)} health=UP ev=[dgx_up]"
    assert "recovery" not in line


def test_format_digest_multiple_events_joined():
    ts = 1790000200.0
    entry = {"ts": ts, "health": "UP",
             "events": ["dgx_up", "recovered:proj-a:001"]}
    line = wd.format_digest([entry], ts + 1).splitlines()[1]
    assert line == f"{_t(ts)} health=UP ev=[dgx_up, recovered:proj-a:001]"


def test_format_digest_truncates_to_max_entries():
    ts0 = 1790000300.0
    entries = [{"ts": ts0 + i, "health": "DOWN",
                "events": [f"tick{i}"]} for i in range(7)]
    lines = wd.format_digest(entries, ts0 + 700).splitlines()
    # header + truncation note + 5 entry lines + footer
    assert len(lines) == 8
    assert "7" in lines[1]  # the note names the total count
    # the 5 SHOWN entries are the most RECENT ones (chronological order)
    shown = [l for l in lines[2:-1]]
    assert len(shown) == 5
    # the OLDEST two were dropped, the newest is last
    joined = " ".join(shown)
    assert "tick0" not in joined and "tick1" not in joined
    assert "tick6" in shown[-1]


def test_format_digest_empty_returns_empty():
    assert wd.format_digest([], 1790000000.0) == ""


def test_format_digest_hides_chronic_session_noise():
    # dead/stale sessions emit capture_failed/unresolvable on EVERY tick
    # forever (the chronic CAPTURE_FAILED open problem) — they must not
    # drown the outage signal in the digest; notable() still keys on
    # health/events/recovery, this is display filtering only.
    ts = 1790000500.0
    entry = {"ts": ts, "health": "DOWN",
             "events": ["dgx_down", "capture_failed:proj-x:003",
                        "unresolvable:proj-y:004"]}
    line = wd.format_digest([entry], ts + 5).splitlines()[1]
    assert line == f"{_t(ts)} health=DOWN ev=[dgx_down]"


# --- state file -----------------------------------------------------------------


def test_state_roundtrip(cc_home):
    p = wd.state_path()
    wd.save_state(p, {"last_ts": 123.0, "last_push_ts": 456.0})
    assert wd.load_state(p) == {"last_ts": 123.0, "last_push_ts": 456.0}
    assert json.loads(p.read_text())["last_ts"] == 123.0


def test_state_missing_returns_defaults(cc_home):
    assert wd.load_state(wd.state_path()) == {
        "last_ts": None, "last_push_ts": None}


def test_state_corrupt_returns_defaults(cc_home):
    p = wd.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json{{", encoding="utf-8")
    assert wd.load_state(p) == {"last_ts": None, "last_push_ts": None}


# --- read_new_entries -------------------------------------------------------------


def _write_log(cc_home, entries):
    log = paths.logs_dir() / "watchdog.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return log


def test_read_new_entries_filters_since_last_ts(cc_home):
    log = _write_log(cc_home, [{"ts": 1.0}, {"ts": 2.0}, {"ts": 3.0}])
    out = wd.read_new_entries(log, 1.0)
    assert [e["ts"] for e in out] == [2.0, 3.0]


def test_read_new_entries_missing_file_is_empty(cc_home):
    assert wd.read_new_entries(paths.logs_dir() / "watchdog.log", 0.0) == []


def test_read_new_entries_skips_garbage_lines(cc_home):
    log = paths.logs_dir() / "watchdog.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("garbage line\n" + json.dumps({"ts": 9.0}) + "\n",
                   encoding="utf-8")
    out = wd.read_new_entries(log, 0.0)
    assert [e["ts"] for e in out] == [9.0]


# --- push_allowed -----------------------------------------------------------------


def test_push_allowed_first_push():
    assert wd.push_allowed(None, 1000.0, 300.0) is True


def test_push_blocked_within_cap():
    # 250 s since the last push < 300 s cap → still inside the window
    assert wd.push_allowed(750.0, 1000.0, 300.0) is False


def test_push_allowed_at_and_after_cap():
    # 299.99 s < cap → blocked; exactly 300 s = five full minutes → over
    assert wd.push_allowed(700.0, 999.99, 300.0) is False
    assert wd.push_allowed(700.0, 1000.0, 300.0) is True
    assert wd.push_allowed(700.0, 5000.0, 300.0) is True


# --- run_once ----------------------------------------------------------------------

NOW = 1790001000.0


def _seed_state(cc_home, last_ts, last_push_ts=None):
    p = wd.state_path()
    wd.save_state(p, {"last_ts": last_ts, "last_push_ts": last_push_ts})
    return p


def _fake_inject(results=None):
    calls = []

    def inject(target, text):
        calls.append((target, text))
        if results is None:
            return True
        try:
            return results.pop(0)
        except IndexError:
            return True

    return inject, calls


def _run(cc_home, *, state=None, log_entries=(), collect=(),
         sessions=None, inject_results=None, cfg_over=None):
    """A full run_once with the state/log seeded and I/O faked."""
    if state is not None:
        _seed_state(cc_home, state[0], state[1])
    if log_entries:
        p = paths.logs_dir() / "watchdog.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for e in log_entries:
                f.write(json.dumps(e) + "\n")
    inject, calls = _fake_inject(inject_results)
    res = wd.run_once(
        now=NOW,
        list_sessions=lambda: (sessions if sessions is not None
                               else []),
        collect=lambda: list(collect),
        ledger_ts=lambda sdir: 999.0,
        display=inject,  # Phase 19: the display callable (was inject=)
        cfg_over=cfg_over,
    )
    return res, calls


def test_run_once_first_run_seeds_no_backlog(cc_home):
    # an old outage in the log must NOT be replayed on first run
    log = paths.logs_dir() / "watchdog.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(
        {"ts": NOW - 3600, "health": "DOWN", "events": ["dgx_down"]}) + "\n",
        encoding="utf-8")
    inject, calls = _fake_inject()
    res = wd.run_once(now=NOW, list_sessions=lambda: [], collect=lambda: [],
                      ledger_ts=lambda sdir: 999.0, display=inject)
    assert res["scanned"] == 0 and res["notable"] == 0
    assert res["pushed"] is False and calls == []
    st = wd.load_state(wd.state_path())
    assert st["last_ts"] == NOW  # seeded to now, not the old ts
    assert st["last_push_ts"] is None


def test_run_once_pushes_notable_to_most_recent_pane(cc_home):
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b")
    b = _cand("proj-b", 2, "WARNED", "cc-proj-b-1c2d")
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 60, "health": "DOWN",
                      "recovery": {"ran": True, "exit": 0,
                                   "elapsed_s": 15.2},
                      "events": ["dgx_down"]}],
        collect=[a, b],
        sessions=["cc-proj-a-0a9b", "cc-proj-b-1c2d"],
    )
    # the ledger_ts fake gives both 999.0 — tie keeps the first in order,
    # so the a-pane must win here (deterministic)
    assert res == {"scanned": 1, "notable": 1,
                   "target": "cc-proj-a-0a9b", "pushed": True,
                   "skipped_busy": False}
    assert len(calls) == 1
    target, text = calls[0]
    assert target == "cc-proj-a-0a9b"
    assert "health=DOWN recovery(exit=0,15s) ev=[dgx_down]" in text
    assert "watchdog digest" in text
    st = wd.load_state(wd.state_path())
    assert st["last_ts"] == NOW - 60 and st["last_push_ts"] == NOW


def test_run_once_no_push_within_cap_but_file_logged(cc_home):
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b")
    res, calls = _run(
        cc_home,
        state=(NOW - 120, NOW - 60),  # pushed 60 s ago < 300 s cap
        log_entries=[{"ts": NOW - 30, "health": "DOWN",
                      "events": ["dgx_down"]}],
        collect=[a],
        sessions=["cc-proj-a-0a9b"],
    )
    assert res["notable"] == 1 and res["pushed"] is False
    assert calls == []
    # the digest still landed in the always-appended file
    content = wd.digest_log_path().read_text(encoding="utf-8")
    assert "health=DOWN" in content
    st = wd.load_state(wd.state_path())
    assert st["last_ts"] == NOW - 30  # advanced, no re-scan next run
    assert st["last_push_ts"] == NOW - 60  # unchanged


def test_run_once_no_target_skips_injection(cc_home):
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 30, "health": "DOWN"}],
        collect=[],  # no live sessions
    )
    assert res["pushed"] is False and calls == []
    assert wd.digest_log_path().is_file()  # still file-logged


def test_run_once_inject_failure_records_no_push(cc_home):
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b")
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 30, "health": "DOWN"}],
        collect=[a],
        sessions=["cc-proj-a-0a9b"],
        inject_results=[False],
    )
    assert res["pushed"] is False and len(calls) == 1
    st = wd.load_state(wd.state_path())
    assert st["last_push_ts"] is None  # retry on the next run


def test_run_once_quiet_entries_advance_last_ts_without_digest(cc_home):
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 30, "health": "UP", "events": []}],
    )
    assert res == {"scanned": 1, "notable": 0,
                   "target": None, "pushed": False,
                   "skipped_busy": False}
    assert calls == []
    assert not wd.digest_log_path().exists()
    assert wd.load_state(wd.state_path())["last_ts"] == NOW - 30


def test_run_once_busy_target_skips_inject_and_holds_last_ts(cc_home):
    # the picked target is mid-turn: do NOT queue the digest in its input
    # box (the user wants it ACTIONED now). Skip the injection, keep it
    # file-logged, and hold last_ts back so the next 2-min tick retries.
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=True)
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 60, "health": "DOWN",
                      "events": ["dgx_down"]}],
        collect=[a],
        sessions=["cc-proj-a-0a9b"],
    )
    assert res == {"scanned": 1, "notable": 1,
                   "target": "cc-proj-a-0a9b", "pushed": False,
                   "skipped_busy": True}
    assert calls == []  # no tmux injection
    # the digest is still always file-logged
    assert "health=DOWN" in wd.digest_log_path().read_text(encoding="utf-8")
    st = wd.load_state(wd.state_path())
    assert st["last_ts"] == NOW - 120  # held back, NOT advanced to NOW-60
    assert st["last_push_ts"] is None


def test_run_once_idle_target_injects_and_advances(cc_home):
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=False)
    res, calls = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 60, "health": "DOWN",
                      "events": ["dgx_down"]}],
        collect=[a],
        sessions=["cc-proj-a-0a9b"],
    )
    assert res == {"scanned": 1, "notable": 1,
                   "target": "cc-proj-a-0a9b", "pushed": True,
                   "skipped_busy": False}
    assert len(calls) == 1 and calls[0][0] == "cc-proj-a-0a9b"
    st = wd.load_state(wd.state_path())
    assert st["last_ts"] == NOW - 60 and st["last_push_ts"] == NOW


def test_run_once_busy_then_idle_retries_next_tick(cc_home):
    # tick 1: target mid-turn → held back. tick 2: same target idle → the
    # SAME notable (still > the held last_ts) is re-read and injected.
    busy = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=True)
    idle = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=False)
    res1, calls1 = _run(
        cc_home,
        state=(NOW - 120, None),
        log_entries=[{"ts": NOW - 60, "health": "DOWN",
                      "events": ["dgx_down"]}],
        collect=[busy],
        sessions=["cc-proj-a-0a9b"],
    )
    assert res1["pushed"] is False and calls1 == []
    assert wd.load_state(wd.state_path())["last_ts"] == NOW - 120
    res2, calls2 = _run(cc_home, collect=[idle],
                        sessions=["cc-proj-a-0a9b"])
    assert res2["pushed"] is True and len(calls2) == 1
    assert calls2[0][0] == "cc-proj-a-0a9b"
    assert wd.load_state(wd.state_path())["last_ts"] == NOW - 60


# --- show_popup (Phase 19: display-popup — a LOG, never a prompt) ------------------
#
# The digest is shown via tmux display-popup on the target client for ~8
# s and closed from the daemon side. The input box is NEVER touched, so
# a digest can never become a queued model turn. Popups CANNOT display on
# a control client (tmux -CC — tmux's own regress test) — no client →
# log-only (False). The opener is a DIRECT subprocess.Popen: the
# display-popup <cmd> CLI blocks until the popup closes, so it must not
# go through runner (tests monkeypatch it separately).


class _P:
    def __init__(self, code, out=b""):
        self.returncode = code
        self.stdout = out
        self.stderr = b""


def _digest_tmp():
    return paths.runtime_dir() / "watchdog-digest-paste.txt"


def _fake_popen(monkeypatch, records, raise_exc=None, result=0):
    """subprocess.Popen stand-in: records the opener cmd and the tmp
    file content (at Popen time); wait() returns ``result``."""

    def fake(cmd, **kw):
        if raise_exc is not None:
            raise raise_exc
        rec = {"cmd": cmd, "tmp_text": None}
        for a in cmd:
            if a.startswith("cat "):
                p = a.split("cat ", 1)[1].split(";")[0]
                rec["tmp_text"] = Path(p).read_text(encoding="utf-8")
                break
        records.append(rec)

        class _Opener:
            def wait(self, timeout=None):
                return result

        return _Opener()

    monkeypatch.setattr(wd.subprocess, "Popen", fake)
    return records


def _client_runner(records, target_client=None, any_client=None,
                   target_rc=0, any_rc=0):
    """runner stand-in answering list-clients (target-specific and
    generic) plus the close call; records every cmd."""

    def runner(cmd, **kw):
        records.append(cmd)
        if cmd[1] == "list-clients":
            if "-t" in cmd:
                return _P(target_rc, (target_client or "").encode())
            return _P(any_rc, (any_client or "").encode())
        return _P(0)

    return runner


def test_show_popup_opens_sleeps_closes(cc_home, monkeypatch):
    runner_calls = []
    popen_calls = []
    slept = []
    _fake_popen(monkeypatch, popen_calls)
    runner = _client_runner(runner_calls, target_client="dev/pts-1")
    ok = wd.show_popup("cc-proj-a-0a9b", "line1\nline2", runner=runner,
                       sleep=lambda s: slept.append(s))
    assert ok is True
    assert popen_calls[0]["cmd"] == [
        "tmux", "display-popup", "-c", "dev/pts-1",
        "-t", "cc-proj-a-0a9b", "-T", wd.POPUP_TITLE,
        "-w", "110", "-h", "16",
        f"cat {_digest_tmp()}; sleep 8"]
    # the digest text is in the tmp file AT Popen time, cleaned after
    assert popen_calls[0]["tmp_text"] == "line1\nline2"
    assert not _digest_tmp().exists()
    assert slept == [8.0]
    assert runner_calls == [
        ["tmux", "list-clients", "-t", "cc-proj-a-0a9b",
         "-F", "#{client_name}"],
        ["tmux", "display-popup", "-x", "-c", "dev/pts-1"],
    ]


def test_show_popup_prefers_target_specific_client(cc_home, monkeypatch):
    runner_calls = []
    popen_calls = []
    _fake_popen(monkeypatch, popen_calls)
    runner = _client_runner(runner_calls, target_client="dev/pts-1",
                            any_client="dev/pts-2")
    ok = wd.show_popup("cc-proj-a-0a9b", "t", runner=runner,
               sleep=lambda s: None)
    assert ok is True
    cmd = popen_calls[0]["cmd"]
    assert cmd[cmd.index("-c") + 1] == "dev/pts-1"
    # the generic list-clients (no -t) was never needed
    assert [c for c in runner_calls if c[1] == "list-clients"] == [
        ["tmux", "list-clients", "-t", "cc-proj-a-0a9b",
         "-F", "#{client_name}"]]


def test_show_popup_falls_back_to_any_client_on_target_rc_nonzero(
        cc_home, monkeypatch):
    # list-clients -t rc != 0 (no client attached to the session) →
    # the generic fallback
    runner_calls = []
    popen_calls = []
    _fake_popen(monkeypatch, popen_calls)
    runner = _client_runner(runner_calls, target_client="dev/pts-1",
                            target_rc=1, any_client="dev/pts-9")
    ok = wd.show_popup("cc-proj-a-0a9b", "t", runner=runner,
               sleep=lambda s: None)
    assert ok is True
    cmd = popen_calls[0]["cmd"]
    assert cmd[cmd.index("-c") + 1] == "dev/pts-9"


def test_show_popup_no_client_is_log_only(cc_home, monkeypatch):
    # no client anywhere (control client / detached) → False, no Popen,
    # tmp cleaned on the early return
    popen_calls = []
    _fake_popen(monkeypatch, popen_calls)
    runner = _client_runner([], target_client="", any_client="\n")
    ok = wd.show_popup("cc-proj-a-0a9b", "t", runner=runner,
               sleep=lambda s: None)
    assert ok is False
    assert popen_calls == []
    assert not _digest_tmp().exists()


def test_show_popup_popen_raises_is_false_and_cleans_tmp(cc_home, monkeypatch):
    popen_calls = []
    _fake_popen(monkeypatch, popen_calls, raise_exc=OSError("no tmux"))
    runner = _client_runner([], target_client="dev/pts-1")
    ok = wd.show_popup("cc-proj-a-0a9b", "t", runner=runner,
               sleep=lambda s: None)
    assert ok is False
    assert popen_calls == []
    assert not _digest_tmp().exists()


def test_show_popup_uses_env_tmux_bin(cc_home, monkeypatch):
    runner_calls = []
    popen_calls = []
    _fake_popen(monkeypatch, popen_calls)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", "/opt/homebrew/bin/tmux")
    runner = _client_runner(runner_calls, target_client="dev/pts-1")
    ok = wd.show_popup("cc-proj-a-0a9b", "t", runner=runner,
               sleep=lambda s: None)
    assert ok is True
    assert all(c[0] == "/opt/homebrew/bin/tmux" for c in runner_calls)
    assert popen_calls[0]["cmd"][0] == "/opt/homebrew/bin/tmux"


# --- plist / launchctl ------------------------------------------------------------------


def test_digest_plist_render_shape(cc_home, tmp_path):
    xml = wd.plist_render("/usr/local/bin/cc-continuity", tmp_path / "home")
    assert "<string>com.claude-continuity.watchdog-digest</string>" in xml
    assert "<string>/usr/local/bin/cc-continuity</string>" in xml
    assert "<string>watchdog</string>" in xml
    assert "<string>digest</string>" in xml
    assert "<string>run</string>" in xml
    assert "<key>RunAtLoad</key>\n  <true/>" in xml
    assert "<key>StartInterval</key>\n  <integer>120</integer>" in xml
    assert "KeepAlive" not in xml  # PERIODIC, not a resident
    assert str(tmp_path / "home") in xml


def test_digest_plist_bakes_tmux_bin(cc_home, tmp_path):
    xml = wd.plist_render("/usr/local/bin/cc-continuity", tmp_path / "home",
                          tmux_bin="/opt/homebrew/bin/tmux")
    assert "<key>CC_CONTINUITY_TMUX_BIN</key>" in xml
    assert "<string>/opt/homebrew/bin/tmux</string>" in xml
    xml_none = wd.plist_render("/usr/local/bin/cc-continuity",
                               tmp_path / "home")
    assert "CC_CONTINUITY_TMUX_BIN" not in xml_none


def test_digest_install_uninstall_with_fake_runner(cc_home, tmp_path,
                                                   monkeypatch):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return _P(0)

    home = tmp_path / "home"
    monkeypatch.setattr(wd.shutil, "which",
                        lambda name: "/bin/cc-continuity")
    p = wd.install_plist(home, runner=runner)
    assert p.is_file()
    assert calls[0][:3] == ["launchctl", "bootstrap", f"gui/{os.getuid()}"]
    wd.uninstall_plist(home, runner=runner)
    assert not p.exists()
    assert calls[1][:3] == ["launchctl", "bootout",
                            f"gui/{os.getuid()}/"
                            "com.claude-continuity.watchdog-digest"]


def test_digest_install_plist_refuses_without_binary(cc_home, tmp_path,
                                                     monkeypatch):
    monkeypatch.setattr(wd.shutil, "which", lambda name: None)
    with pytest.raises(wd.DigestError):
        wd.install_plist(tmp_path / "home", runner=lambda *a, **kw: _P(0))


# --- config + CLI -----------------------------------------------------------------------


def test_config_default_push_cap():
    from cc_continuity import config
    assert config.DEFAULTS["watchdog_digest_push_cap_s"] == 300


def test_cli_watchdog_digest_run_prints_summary(cc_home, capsys,
                                                monkeypatch):
    from cc_continuity import cli
    res = {"scanned": 1, "notable": 1, "target": None, "pushed": False,
           "skipped_busy": False}
    monkeypatch.setattr(wd, "run_once", lambda **kw: res)
    rc = cli.main(["watchdog", "digest", "run"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == res


# --- pick_target --------------------------------------------------------------------


def _cand(key="proj-a", nnn=1, st="ACTIVE", tmux="cc-proj-a-0a9b",
          turn_active=False):
    """A (key, nnn, rec) candidate — pick_target is pure over rec dicts."""
    rec = {"state": st, "turn_active": turn_active}
    if tmux:
        rec["tmux_session"] = tmux
    return (key, nnn, rec)


def test_pick_target_most_recent_wins():
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b")
    b = _cand("proj-b", 2, "WARNED", "cc-proj-b-1c2d")
    target = wd.pick_target([(a[0], a[1], a[2], 100.0),
                             (b[0], b[1], b[2], 200.0)],
                            ["cc-proj-a-0a9b", "cc-proj-b-1c2d"])
    assert target == "cc-proj-b-1c2d"


def test_pick_target_skips_sessions_without_ledger_ts():
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b")
    b = _cand("proj-b", 2, "ACTIVE", "cc-proj-b-1c2d")
    target = wd.pick_target([(a[0], a[1], a[2], None),
                             (b[0], b[1], b[2], 200.0)],
                            ["cc-proj-b-1c2d"])
    assert target == "cc-proj-b-1c2d"


def test_pick_target_skips_ineligible_state():
    a = _cand("proj-a", 1, "CHECKPOINT_VERIFIED", "cc-proj-a-0a9b")
    b = _cand("proj-b", 2, "ACTIVE", "cc-proj-b-1c2d")
    target = wd.pick_target([(a[0], a[1], a[2], 500.0),
                             (b[0], b[1], b[2], 100.0)],
                            ["cc-proj-b-1c2d"])
    assert target == "cc-proj-b-1c2d"


def test_pick_target_unresolvable_returns_none():
    a = _cand("proj-a", 1, "ACTIVE", tmux=None)
    # two tmux sessions match the prefix → resolve_pane refuses
    target = wd.pick_target([(a[0], a[1], a[2], 100.0)],
                            ["cc-proj-a-0a9b", "cc-proj-a-77ff"])
    assert target is None


def test_pick_target_no_candidates_returns_none():
    assert wd.pick_target([], ["cc-proj-a-0a9b"]) is None


# --- _target_turn_active ---------------------------------------------------------


def test_target_turn_active_busy_rec_returns_true():
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=True)
    assert wd._target_turn_active([(a[0], a[1], a[2], 100.0)]) is True


def test_target_turn_active_idle_rec_returns_false():
    a = _cand("proj-a", 1, "ACTIVE", "cc-proj-a-0a9b", turn_active=False)
    assert wd._target_turn_active([(a[0], a[1], a[2], 100.0)]) is False


def test_target_turn_active_no_candidates_returns_false():
    assert wd._target_turn_active([]) is False


def test_target_turn_active_skips_ineligible_and_stale():
    # a busy rec that is ineligible or has no ledger ts is not the target
    a = _cand("proj-a", 1, "CHECKPOINT_VERIFIED", "cc-proj-a-0a9b",
              turn_active=True)
    assert wd._target_turn_active([(a[0], a[1], a[2], None)]) is False
