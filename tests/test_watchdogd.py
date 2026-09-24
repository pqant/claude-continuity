"""watchdogd.py: Phase 10 daemon (P10-D12). tick() with fakes: live
session filter, pane resolution (field / fallback / unresolvable),
FROZEN tick counting, nudge → verify → recovered, SEND_ENTER, GIVE_UP,
freeze dedup, rotation race, tick-exception survival, plist render,
run_forever disabled exit 0."""

import json
import os

import pytest

from cc_continuity import ledger, notify, paths, state
from cc_continuity import watchdogd
from cc_continuity.watchdog import HealthState
from cc_continuity.watchdogd import TickCtx

PROMPT_PANE = "some output line\n❯ "
BUSY_PANE = "line one\nline two"  # not at prompt


class FakeIO:
    """Injectable I/O bag: mutable ``pane``/``ok`` let a test drive
    ticks; ``sends`` records every send-keys call."""

    def __init__(self, pane=PROMPT_PANE, health_ok=True, sessions=None):
        self.pane = pane
        self.ok = health_ok
        self.sessions = sessions
        self.sends = []
        self.probe_calls = 0
        self.recover_calls = 0
        self.recover_result = None  # None = no recovery configured

    def probe(self):
        self.probe_calls += 1
        if isinstance(self.ok, Exception):
            raise self.ok
        if isinstance(self.ok, list):
            # a list is a call sequence; the last value repeats
            return self.ok[min(self.probe_calls - 1, len(self.ok) - 1)]
        return self.ok

    def recover(self):
        self.recover_calls += 1
        return self.recover_result

    def capture(self, target):
        return self.pane

    def list_sessions(self):
        return self.sessions

    def send(self, target, *keys):
        self.sends.append((target, *keys))
        return True


def _ctx(io, cfg_over=None, now=10_000.0, down=False):
    """A TickCtx on a liveness+health cfg; ``down`` pre-feeds two failed
    probes (cold start UNKNOWN → DOWN)."""
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300,
           "watchdog_stale_minutes": 45, "watchdog_max_nudges": 3,
           "watchdog_nudge_text": "continue",
           "watchdog_health_url": "http://127.0.0.1:1/health"}
    cfg.update(cfg_over or {})
    ctx = TickCtx(cfg=cfg, health=HealthState(), episodes={},
                  prev_captures={}, froze=set(), probe=io.probe,
                  capture=io.capture, list_sessions=io.list_sessions,
                  send=io.send, recover=io.recover, now=now)
    if down:
        ctx.health.feed(False, now)
        ctx.health.feed(False, now)
    return ctx


def _mk(key="proj-a", nnn=1, st="ACTIVE", tmux="cc-proj-a-0a9b",
        turn_active=False):
    state.create_session_state(key, f"{key}-{nnn:03d}", nnn,
                               session_id=f"sid-{key}-{nnn}",
                               initial_state=st)
    if tmux:
        state.update_fields(key, nnn, tmux_session=tmux)
    if turn_active:
        state.update_fields(key, nnn, turn_active=True)
    return paths.session_dir(key, nnn)


def _stale_prompt(sdir, now=10_000.0, age=50 * 60):
    """Ledger whose last chain entry is older than the stale threshold."""
    ledger.append(sdir, {"type": "user_prompt", "ts": now - age})


def _capture_pushes(monkeypatch):
    pushes = []

    def fake(cfg, event, *, slug, nnn=0, dedup_key=None, wait=False,
             **fields):
        pushes.append((event, slug, nnn, fields))
        return True

    monkeypatch.setattr(notify, "safe_send", fake)
    return pushes


# --- run_forever -------------------------------------------------------------


def test_run_forever_disabled_exits_0(cc_home):
    io = FakeIO()
    cfg = {"watchdog_enabled": False, "watchdog_interval_s": 1}
    rc = watchdogd.run_forever(cfg, max_ticks=5, sleep=lambda s: None,
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send)
    assert rc == 0
    assert io.probe_calls == 0  # the loop never started


def test_run_forever_runs_max_ticks(cc_home):
    io = FakeIO(health_ok=True)
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300}
    rc = watchdogd.run_forever(cfg, max_ticks=3, sleep=lambda s: None,
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send)
    assert rc == 0
    assert io.probe_calls == 3
    assert (paths.logs_dir() / "watchdog.log").is_file()


def test_run_forever_tick_exception_survives(cc_home):
    # a bad config value crashes the tick — the loop must survive
    io = FakeIO(health_ok=True)
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1))
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300,
           "watchdog_stale_minutes": "not-a-number",
           "watchdog_max_nudges": 3, "watchdog_nudge_text": "continue",
           "watchdog_health_url": ""}
    rc = watchdogd.run_forever(cfg, max_ticks=2, sleep=lambda s: None,
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send)
    assert rc == 0
    assert io.probe_calls == 2  # continued past the crashed tick
    assert (paths.logs_dir() / "errors.log").is_file()


# --- collect / resolve -------------------------------------------------------


def test_collect_live_sessions_filters(cc_home):
    _mk(nnn=1, st="ACTIVE")
    _mk(nnn=2, st="ROTATED")
    _mk(nnn=3, st="ACTIVE")
    state.update_fields("proj-a", 3, off=True)
    s4 = paths.session_dir("proj-a", 4)
    s4.mkdir(parents=True)
    (s4 / "state.json").write_text("{broken", encoding="utf-8")  # unreadable → skip
    got = {(k, n) for k, n, _ in watchdogd.collect_live_sessions()}
    assert got == {("proj-a", 1)}


def test_resolve_pane_field_wins():
    rec = {"tmux_session": "cc-proj-a-0a9b"}
    assert watchdogd.resolve_pane(rec, "proj-a",
                                  ["cc-proj-a-other"]) == "cc-proj-a-0a9b"


def test_resolve_pane_fallback_exactly_one():
    assert watchdogd.resolve_pane({}, "proj-a",
                                  ["cc-other-x",
                                   "cc-proj-a-0a9b"]) == "cc-proj-a-0a9b"


def test_resolve_pane_unresolvable():
    assert watchdogd.resolve_pane({}, "proj-a", []) is None
    assert watchdogd.resolve_pane({}, "proj-a",
                                  ["cc-proj-a-1",
                                   "cc-proj-a-2"]) is None
    assert watchdogd.resolve_pane({}, "proj-a", None) is None


def test_last_ledger_ts_excludes_watchdog_entries(cc_home):
    sdir = _mk()
    ledger.append(sdir, {"type": "user_prompt", "ts": 100.0})
    assert watchdogd.last_ledger_ts(sdir) == 100.0
    # the daemon's own write is NOT chain activity (P10-P9 refinement)
    ledger.append(sdir, {"type": "watchdog_nudge", "n": 1, "ts": 200.0})
    assert watchdogd.last_ledger_ts(sdir) == 100.0
    assert watchdogd.last_ledger_ts(paths.session_dir("proj-a", 99)) \
        is None  # no ledger → None (stale)


def test_probe_health_empty_url_is_liveness_only():
    assert watchdogd.probe_health({"watchdog_health_url": ""}) is True


# --- recover_network (P10 network recovery) ----------------------------------


class _FakeRecoverRunner:
    """Stands in for ``subprocess.run`` (shell=True) — records the call,
    returns a fixed result, or raises like a missing binary."""

    def __init__(self, stdout=b"", stderr=b"", rc=0, exc=None):
        self.stdout = stdout
        self.stderr = stderr
        self.rc = rc
        self.exc = exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc is not None:
            raise self.exc

        class _P:
            pass
        p = _P()
        p.stdout = self.stdout
        p.stderr = self.stderr
        p.returncode = self.rc
        return p


def test_recover_network_runs_and_captures_stdout():
    r = _FakeRecoverRunner(stdout=b"[DGX] Network recovery completed.\n")
    rec = watchdogd.recover_network(
        {"watchdog_network_recovery_cmd":
         "sudo -n /usr/local/libexec/dgx-network-recovery"}, runner=r)
    assert rec["ran"] is True
    assert rec["exit"] == 0
    assert rec["out"] == "[DGX] Network recovery completed."
    assert len(r.calls) == 1
    assert r.calls[0][0] == \
        "sudo -n /usr/local/libexec/dgx-network-recovery"
    assert r.calls[0][1].get("shell") is True


def test_recover_network_includes_stderr():
    r = _FakeRecoverRunner(stdout=b"line1\n", stderr=b"err1\n")
    rec = watchdogd.recover_network(
        {"watchdog_network_recovery_cmd": "true"}, runner=r)
    assert rec["out"] == "line1\nerr1"


def test_recover_network_empty_cmd_is_noop():
    r = _FakeRecoverRunner()
    rec = watchdogd.recover_network(
        {"watchdog_network_recovery_cmd": ""}, runner=r)
    assert rec["ran"] is False
    assert rec["exit"] is None
    assert r.calls == []  # nothing executed


def test_recover_network_missing_cmd_is_noop():
    r = _FakeRecoverRunner()
    rec = watchdogd.recover_network({}, runner=r)
    assert rec["ran"] is False
    assert r.calls == []


def test_recover_network_nonzero_exit_reported():
    r = _FakeRecoverRunner(rc=2)
    rec = watchdogd.recover_network(
        {"watchdog_network_recovery_cmd": "x"}, runner=r)
    assert rec["ran"] is True
    assert rec["exit"] == 2


def test_recover_network_runner_raises_degrades():
    r = _FakeRecoverRunner(exc=OSError("no sudo"))
    rec = watchdogd.recover_network(
        {"watchdog_network_recovery_cmd": "x"}, runner=r)
    assert rec["ran"] is True
    assert rec["exit"] is None
    assert "failed to start" in rec["out"]


# --- tick + recovery (P10 network-recovery wiring) ---------------------------


def test_tick_failed_probe_recovers_then_up(cc_home):
    # fail → recovery ran → re-probe ok: the tick counts as healthy
    io = FakeIO(health_ok=[False, True])
    io.recover_result = {"ran": True, "exit": 0, "elapsed_s": 14.8,
                         "out": "[DGX] Network recovery completed."}
    ctx = _ctx(io, cfg_over={"watchdog_network_recovery_cmd":
                             "sudo -n /usr/local/libexec/dgx-network-recovery"})
    line = watchdogd.tick(ctx)
    assert io.probe_calls == 2        # probe + re-probe
    assert io.recover_calls == 1
    assert "dgx_down" not in line["events"]
    assert ctx.health.status == "UP"
    assert line["recovery"] == {"ran": True, "exit": 0, "elapsed_s": 14.8}
    log = (paths.runtime_dir() / "watchdog-recovery.log").read_text(
        encoding="utf-8")
    assert "[DGX] Network recovery completed." in log


def test_tick_recovery_fails_still_down(cc_home, monkeypatch):
    # fail → recovery ran → re-probe still fails: counts as a real fail
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=[False, False])
    io.recover_result = {"ran": True, "exit": 1, "elapsed_s": 15.0,
                         "out": "[DGX] route still missing."}
    ctx = _ctx(io, cfg_over={"watchdog_network_recovery_cmd": "x"})
    ctx.health.feed(False, ctx.now)   # first strike already consumed
    line = watchdogd.tick(ctx)
    assert io.probe_calls == 2
    assert ctx.health.status == "DOWN"
    assert "dgx_down" in line["events"]
    assert any(p[0] == "dgx_down" for p in pushes)


def test_tick_healthy_probe_never_recovers(cc_home):
    io = FakeIO(health_ok=True)
    ctx = _ctx(io)
    line = watchdogd.tick(ctx)
    assert io.probe_calls == 1
    assert io.recover_calls == 0
    assert "recovery" not in line


def test_tick_recovery_noop_does_not_reprobe(cc_home):
    # empty cmd → ran: False → no second chance, just one probe
    io = FakeIO(health_ok=False)
    io.recover_result = {"ran": False, "exit": None, "elapsed_s": 0.0,
                         "out": ""}
    ctx = _ctx(io, cfg_over={"watchdog_network_recovery_cmd": ""})
    line = watchdogd.tick(ctx)
    assert io.probe_calls == 1
    assert io.recover_calls == 1
    assert line["recovery"]["ran"] is False
    assert "dgx_down" not in line["events"]  # 1 fail < 2-strike debounce


# --- tick: branches ----------------------------------------------------------


def test_tick_alive_when_ledger_fresh(cc_home):
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    ledger.append(sdir, {"type": "user_prompt", "ts": 9_900.0})
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "ALIVE"
    assert io.sends == []


def test_tick_turn_dead_nudges_when_up(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)  # 50 min > 45 min threshold
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "TURN_DEAD"
    assert io.sends == [("cc-proj-a-0a9b", "continue", "Enter")]
    assert [p[0] for p in pushes] == ["turn_dead"]
    assert ctx.episodes[("proj-a", 1)].n == 1
    entries = ledger.read(sdir)
    assert entries[-1]["type"] == "watchdog_nudge"
    assert entries[-1]["n"] == 1


def test_tick_turn_dead_down_waits_then_up_nudges(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=False)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0, down=True)
    assert ctx.health.status == "DOWN"
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "TURN_DEAD"
    assert io.sends == []  # a nudge is a model call — deferred while DOWN
    assert "waiting_for_dgx:proj-a:001" in line["events"]
    assert pushes == []  # no turn_dead push: no nudge was sent

    io.ok = True  # server back
    ctx.now = 10_300.0
    line2 = watchdogd.tick(ctx)
    assert [p[0] for p in pushes] == ["dgx_up", "turn_dead"]
    assert io.sends == [("cc-proj-a-0a9b", "continue", "Enter")]
    assert ctx.episodes[("proj-a", 1)].waiting_health is True


def test_tick_verification_pushes_recovered(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    watchdogd.tick(ctx)  # NUDGE (n=1, nudge_ts=10_000.0)
    # the nudge LANDING: CC records the user prompt (ts > nudge_ts)
    ledger.append(sdir, {"type": "user_prompt", "ts": 10_001.0})
    ctx.now = 10_002.0
    line2 = watchdogd.tick(ctx)
    # verification runs BEFORE classification (binding placement, Task 7
    # interfaces): the open episode verifies on this tick → VERIFIED
    assert line2["sessions"][0]["outcome"] == "VERIFIED"
    assert [p[0] for p in pushes] == ["turn_dead", "turn_recovered"]
    assert ctx.episodes == {}  # episode reset
    types = [e["type"] for e in ledger.read(sdir)]
    assert "watchdog_verified" in types


def test_tick_send_enter_on_unchanged_pending_pane(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    watchdogd.tick(ctx)  # NUDGE: sent text+Enter; the Enter was lost…
    assert io.sends == [("cc-proj-a-0a9b", "continue", "Enter")]
    # …and the text now sits in the input line (the 11:30 case).
    io.pane = "some output line\n❯ continue"
    ctx.now = 10_300.0
    l2 = watchdogd.tick(ctx)
    assert l2["sessions"][0]["outcome"] == "ALIVE"  # capture CHANGED
    assert io.sends[-1] == ("cc-proj-a-0a9b", "continue", "Enter")  # no-op
    ctx.now = 10_600.0
    l3 = watchdogd.tick(ctx)  # now UNCHANGED → at-prompt → TURN_DEAD
    assert l3["sessions"][0]["outcome"] == "TURN_DEAD"
    assert io.sends[-1] == ("cc-proj-a-0a9b", "Enter")  # bare Enter
    assert ctx.episodes[("proj-a", 1)].n == 1  # SEND_ENTER does not count
    assert [p[0] for p in pushes] == ["turn_dead"]  # no resend push


def test_tick_give_up_after_three_unverified(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    for i in range(4):  # NUDGE, RESEND, RESEND, GIVE_UP
        watchdogd.tick(ctx)
        ctx.now += 300.0
    ep = ctx.episodes[("proj-a", 1)]
    assert ep.n == 3
    assert ep.given_up is True
    assert [p[0] for p in pushes] == ["turn_dead", "needs_human"]
    assert io.sends.count(("cc-proj-a-0a9b", "continue", "Enter")) == 3
    entries = ledger.read(sdir)
    assert entries[-1]["type"] == "needs_human"
    assert entries[-1]["source"] == "watchdog"
    # tick 5: DONE — no further action until a human user_prompt
    watchdogd.tick(ctx)
    assert ctx.episodes[("proj-a", 1)].n == 3
    assert [p[0] for p in pushes] == ["turn_dead", "needs_human"]


def test_tick_frozen_pushes_once(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE)  # not at prompt
    _mk(turn_active=False)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    l1 = watchdogd.tick(ctx)  # first tick: no previous capture → WAIT
    assert l1["sessions"][0]["outcome"] == "WAIT"
    l2 = watchdogd.tick(ctx)  # 1 unchanged tick → still WAIT
    assert l2["sessions"][0]["outcome"] == "WAIT"
    l3 = watchdogd.tick(ctx)  # 2 unchanged ticks → FROZEN (P10-P3)
    assert l3["sessions"][0]["outcome"] == "FROZEN"
    assert [p[0] for p in pushes] == ["freeze_detected"]
    assert ledger.read(paths.session_dir("proj-a", 1))[-1]["type"] \
        == "watchdog_freeze"
    l4 = watchdogd.tick(ctx)  # still FROZEN — but the push fires ONCE
    assert l4["sessions"][0]["outcome"] == "FROZEN"
    assert [p[0] for p in pushes] == ["freeze_detected"]


def test_tick_frozen_dedup_seeded_from_ledger(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE)
    sdir = _mk(turn_active=False)
    _stale_prompt(sdir, now=10_000.0)
    ledger.append(sdir, {"type": "watchdog_freeze"})  # pushed pre-restart
    ctx = _ctx(io, now=10_000.0)
    ctx.froze = watchdogd._seed_froze()  # run_forever seeds at startup
    assert ("proj-a", 1) in ctx.froze
    for _ in range(3):
        watchdogd.tick(ctx)
    assert pushes == []  # no re-push after the daemon restart


def test_tick_unresolvable_never_nudges(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True,
                sessions=["cc-proj-a-1", "cc-proj-a-2"])  # ambiguous
    _mk(turn_active=True, tmux=None)  # no hook-recorded field
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "UNRESOLVABLE"
    assert io.sends == [] and pushes == []


def test_tick_capture_failed_skips(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    io.capture = lambda target: None
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "CAPTURE_FAILED"
    assert io.sends == [] and pushes == []


def test_tick_rotation_race_skips_nudge(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    # the rotation lands between the scan and the send: the re-read at
    # send time (P10-D5) must abort the nudge
    monkeypatch.setattr(
        watchdogd.state, "load_state",
        lambda key, nnn: {"state": "ROTATING", "nnn": nnn})
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert io.sends == []
    assert "skip_state:proj-a:001" in line["events"]
    assert pushes == []


# --- plist (P10-D2/P10-P8; real launchd NEVER touched) ------------------------


def test_plist_render_shape(cc_home, tmp_path):
    xml = watchdogd.plist_render("/usr/local/bin/cc-continuity",
                                 tmp_path / "home")
    assert "<string>com.claude-continuity.watchdog</string>" in xml
    assert "<string>/usr/local/bin/cc-continuity</string>" in xml
    assert "<string>watchdog</string>" in xml
    assert "<string>run</string>" in xml
    assert "<key>KeepAlive</key>\n  <true/>" in xml
    assert "<key>RunAtLoad</key>\n  <true/>" in xml
    assert str(tmp_path / "home") in xml


def test_install_uninstall_plist_with_fake_runner(cc_home, tmp_path,
                                                  monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0

    def runner(cmd, **kw):
        calls.append(cmd)
        return FakeProc()

    home = tmp_path / "home"
    monkeypatch.setattr(watchdogd.shutil, "which",
                        lambda name: "/bin/cc-continuity")
    p = watchdogd.install_plist(home, runner=runner)
    assert p.is_file()
    assert calls[0][:3] == ["launchctl", "bootstrap", f"gui/{os.getuid()}"]
    watchdogd.uninstall_plist(home, runner=runner)
    assert not p.exists()
    assert calls[1][:3] == ["launchctl", "bootout",
                            f"gui/{os.getuid()}/"
                            "com.claude-continuity.watchdog"]


def test_install_plist_refuses_without_binary(cc_home, tmp_path,
                                              monkeypatch):
    monkeypatch.setattr(watchdogd.shutil, "which", lambda name: None)
    with pytest.raises(watchdogd.WatchdogError):
        watchdogd.install_plist(tmp_path / "home", runner=lambda *a: None)
