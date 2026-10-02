"""watchdogd.py: Phase 10 daemon (P10-D12). tick() with fakes: live
session filter, pane resolution (field / fallback / unresolvable),
FROZEN tick counting, nudge → verify → recovered, SEND_ENTER, GIVE_UP,
freeze dedup, rotation race, tick-exception survival, plist render,
run_forever disabled exit 0."""

import json
import os
import signal
import subprocess
from datetime import datetime, timezone

import pytest

from cc_continuity import ledger, notify, paths, state, watchdog
from cc_continuity import watchdogd
from cc_continuity.watchdog import HealthState
from cc_continuity.watchdogd import TickCtx

PROMPT_PANE = "some output line\n❯ "
BUSY_PANE = "line one\nline two"  # not at prompt


def _turn_dead_text(n):
    """The Phase 19 plain-form nudge text for ``n`` idle minutes."""
    return ("continue\n\n[cc-continuity watchdog] "
            f"The turn stalled ({n} min idle) — Continue from where you left off.")


class FakeIO:
    """Injectable I/O bag: mutable ``pane``/``ok`` let a test drive
    ticks; ``sends`` records every send-keys call; ``slept`` records
    every settle pause (Phase 19 ESC settle). Phase 21: the exit-hang
    I/O bag (``panes``/``procmap``/``transcript``/``alive_pids``) and
    the ``kills`` record."""

    def __init__(self, pane=PROMPT_PANE, health_ok=True, sessions=None,
                 panes=None, procmap=None, transcript=None,
                 alive_pids=None, kill_result="term",
                 kill_session_result=True, gen_ok=True, process_start=0.0):
        self.pane = pane
        self.ok = health_ok
        self.sessions = sessions
        self.sends = []
        self.slept = []
        self.probe_calls = 0
        self.recover_calls = 0
        self.recover_result = None  # None = no recovery configured
        self.cycle_calls = 0
        self.cycle_result = None
        self.last_cycle_ts_value = None
        self.recorded_cycle = []
        # Phase 21 exit-hang bag (all optional; None = nothing configured)
        self.panes = panes  # target -> list[dict] | None (or a bare list)
        self.procmap = procmap
        self.transcript = transcript  # (ts, etype, text) rows for any path
        self.alive_pids = set() if alive_pids is None else set(alive_pids)
        self.kill_result = kill_result
        self.kills = []
        self.kill_session_result = kill_session_result
        self.kill_session_calls = []
        self.ps_calls = 0
        self.read_calls = []
        # cp-069: process start ts for the start-time guard. 0.0 = an OLD
        # process (the real-hang default, keeps existing tests meaningful);
        # None = ps failure / unknown (the degrade path).
        self.process_start = process_start
        # Phase 22: the generation probe (frozen-turn escalation)
        self.gen_ok = gen_ok
        self.gen_probe_calls = 0

    def list_panes(self, target):
        if self.panes is None:
            return None
        if isinstance(self.panes, dict):
            return self.panes.get(target)
        return self.panes

    def ps_snapshot(self):
        self.ps_calls += 1
        return self.procmap

    def read_transcript(self, path, session_id=None):
        self.read_calls.append((path, session_id))
        t = self.transcript
        if isinstance(t, dict):
            # cp-070: the session's OWN file when present; Phase 21.3:
            # when the recorded session_id is STALE (its <sid>.jsonl
            # absent from the dir) fall back to the dir's newest file —
            # mirrors read_transcript_entries (own-if-present, else newest).
            if session_id and session_id in t:
                return t[session_id]
            return t.get("<newest>")
        return t

    def is_alive(self, pid):
        return pid in self.alive_pids

    def process_start_ts(self, pid):
        return self.process_start

    def kill(self, pid):
        self.kills.append(pid)
        return self.kill_result

    def kill_session(self, target):
        self.kill_session_calls.append(target)
        if isinstance(self.kill_session_result, Exception):
            raise self.kill_session_result
        return bool(self.kill_session_result)

    def sleep(self, s):
        self.slept.append(s)

    def probe(self):
        self.probe_calls += 1
        if isinstance(self.ok, Exception):
            raise self.ok
        if isinstance(self.ok, list):
            # a list is a call sequence; the last value repeats
            return self.ok[min(self.probe_calls - 1, len(self.ok) - 1)]
        return self.ok

    def probe_generation(self):
        self.gen_probe_calls += 1
        if isinstance(self.gen_ok, Exception):
            raise self.gen_ok
        if isinstance(self.gen_ok, list):
            # a list is a call sequence; the last value repeats
            return self.gen_ok[min(self.gen_probe_calls - 1,
                                   len(self.gen_ok) - 1)]
        return self.gen_ok

    def recover(self):
        self.recover_calls += 1
        return self.recover_result

    def cycle(self):
        self.cycle_calls += 1
        return self.cycle_result

    def last_cycle_ts(self):
        return self.last_cycle_ts_value

    def record_cycle_ts(self, ts):
        self.recorded_cycle.append(ts)

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
                  send=io.send, sleep=io.sleep, recover=io.recover,
                  cycle=io.cycle, last_cycle_ts=io.last_cycle_ts,
                  record_cycle_ts=io.record_cycle_ts, now=now,
                  probe_generation=io.probe_generation)
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


def _advancing_now(step: float):
    """A fake clock: the first call returns 0.0, then every call
    advances ``step`` (the Phase D single loop is driven by the fake,
    never the real monotonic time)."""
    i = [0]

    def fake_now():
        v = i[0] * step
        i[0] += 1
        return v

    return fake_now


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
                               now=_advancing_now(305.0),
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send, probe_gen=io.probe_generation)
    assert rc == 0
    assert io.probe_calls == 3
    assert io.gen_probe_calls == 0  # no suspicion → no gen probe
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
                               now=_advancing_now(305.0),
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send, probe_gen=io.probe_generation)
    assert rc == 0
    assert io.probe_calls == 2  # continued past the crashed tick
    assert (paths.logs_dir() / "errors.log").is_file()


def test_run_forever_fast_pass_every_iteration_tick_at_interval(cc_home):
    # Phase 21 contract: the exit-hang fast pass runs EVERY loop
    # iteration (the 5s poll), but the full tick (probe + session scan)
    # only when the interval has elapsed — so a hung /exit dies in ~35s,
    # not ~5min. An eligible live session (turn inactive, resolvable
    # pane) makes every fast pass take ONE ps snapshot.
    io = FakeIO(health_ok=True, sessions=["cc-proj-a-0a9b"])
    _mk(turn_active=False)  # eligible -> one ps per fast pass
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300}
    rc = watchdogd.run_forever(cfg, max_ticks=2, sleep=lambda s: None,
                               now=_advancing_now(5.0),
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send,
                               list_panes=io.list_panes,
                               ps_snapshot=io.ps_snapshot,
                               read_transcript=io.read_transcript,
                               is_alive=io.is_alive, kill=io.kill,
                               kill_session=io.kill_session,
                               probe_gen=io.probe_generation)
    assert rc == 0
    # two full ticks (first immediate at now=0, second at now=300)
    assert io.probe_calls == 2
    # fast pass every iteration: now=0,5,...,300 = 61 iterations
    assert io.ps_calls == 61


def test_run_forever_zombie_check_runs_every_fast_pass(cc_home, monkeypatch):
    # Phase 25: zombie cleanup is a 5 s fast-pass step (like exit-hang),
    # NOT the 300 s full tick — an orphaned off-pane must be reaped in
    # ~5-10 s, not ~7 min. So run_forever must call check_zombie_sessions
    # once per loop iteration (61 at 5 s steps over 300 s), not once per
    # tick (2).
    io = FakeIO(health_ok=True, sessions=["cc-proj-a-0a9b"])
    calls = {"n": 0}
    real = watchdogd.check_zombie_sessions

    def spy(ctx, line):
        calls["n"] += 1
        return real(ctx, line)

    monkeypatch.setattr(watchdogd, "check_zombie_sessions", spy)
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300}
    rc = watchdogd.run_forever(cfg, max_ticks=2, sleep=lambda s: None,
                               now=_advancing_now(5.0),
                               probe=io.probe, capture=io.capture,
                               list_sessions=io.list_sessions,
                               send=io.send, list_panes=io.list_panes,
                               ps_snapshot=io.ps_snapshot,
                               read_transcript=io.read_transcript,
                               is_alive=io.is_alive, kill=io.kill,
                               kill_session=io.kill_session,
                               probe_gen=io.probe_generation)
    assert rc == 0
    assert calls["n"] == 61  # every fast-pass iteration, not 2 full ticks


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


# --- Phase 26 (A fix b): read-side session_end skip --------------------------


def test_session_ended_on_disk_chain_tail(cc_home):
    sdir = _mk()
    assert not watchdogd.session_ended_on_disk("proj-a", 1)  # no ledger
    ledger.append(sdir, {"type": "user_prompt"})
    assert not watchdogd.session_ended_on_disk("proj-a", 1)
    ledger.append(sdir, {"type": "session_end", "reason": "other"})
    assert watchdogd.session_ended_on_disk("proj-a", 1)
    # a ghost daemon write after session_end does NOT un-end it (P10-P9
    # idiom: the daemon's own entries are not chain activity — the 2026-10-01
    # dev 001-036 cohort all end session_end, watchdog_freeze)
    ledger.append(sdir, {"type": "watchdog_freeze"})
    assert watchdogd.session_ended_on_disk("proj-a", 1)
    ledger.append(sdir, {"type": "needs_human", "reason": "x",
                         "source": "watchdog"})
    assert watchdogd.session_ended_on_disk("proj-a", 1)
    # chain activity after session_end (a new turn in the record) → live
    ledger.append(sdir, {"type": "user_prompt"})
    assert not watchdogd.session_ended_on_disk("proj-a", 1)


def test_tick_skips_record_whose_chain_ends_session_end(cc_home, monkeypatch):
    # the 2026-10-01 connector cohort: state ACTIVE/off=False, dead tmux,
    # ledger chain ends session_end → NOT monitored: no session entry, no
    # unresolvable event, no nudge
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, sessions=[])  # dead tmux
    _mk(turn_active=True, tmux="cc-proj-a-dead")
    sdir = paths.session_dir("proj-a", 1)
    ledger.append(sdir, {"type": "session_start"})
    ledger.append(sdir, {"type": "session_end", "reason": "other"})
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"] == []
    assert not [e for e in line["events"] if e.startswith("unresolvable")]
    assert io.sends == [] and pushes == []


def test_tick_still_monitors_record_without_session_end(cc_home, monkeypatch):
    # fail-open direction: a record without a session_end chain tail is
    # monitored exactly as before (a live session is never unwatched)
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, sessions=[])
    _mk(turn_active=True, tmux="cc-proj-a-dead")
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "UNRESOLVABLE"
    assert io.sends == [] and pushes == []


def test_resolve_pane_field_wins_when_live():
    # the field wins over the (ambiguous) prefix fallback — but only while
    # it still exists in the live tmux session list
    rec = {"tmux_session": "cc-proj-a-0a9b"}
    assert watchdogd.resolve_pane(rec, "proj-a",
                                  ["cc-proj-a-0a9b",
                                   "cc-proj-a-other"]) == "cc-proj-a-0a9b"


def test_resolve_pane_stale_field_falls_back_to_live_prefix():
    # regression 2026-09-27 046: the hook-recorded field pointed at a dead
    # tmux session (637d); the live one (9665) existed under the same prefix
    # and must be found instead of returning the stale field verbatim
    rec = {"tmux_session": "cc-proj-a-637d"}
    assert watchdogd.resolve_pane(rec, "proj-a",
                                  ["cc-other-x",
                                   "cc-proj-a-9665"]) == "cc-proj-a-9665"


def test_resolve_pane_stale_field_no_match_is_unresolvable():
    rec = {"tmux_session": "cc-proj-a-637d"}
    assert watchdogd.resolve_pane(rec, "proj-a",
                                  ["cc-other-x"]) is None


def test_resolve_pane_stale_field_no_server_keeps_legacy():
    # tmux server unavailable (sessions is None): existence cannot be
    # verified, so the field is returned as before — the capture then
    # fails and is logged CAPTURE_FAILED (the honest outcome label)
    rec = {"tmux_session": "cc-proj-a-637d"}
    assert watchdogd.resolve_pane(rec, "proj-a", None) == "cc-proj-a-637d"


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


# --- Phase 22 generation probe (frozen-turn escalation) -----------------------


class _FakeGenRunner:
    """Stands in for ``subprocess.run`` for ``probe_generation``: a list of
    fixed per-call results ``(stdout, rc)`` or an Exception (last repeats).
    Records every call — the two-step probe is asserted on the call list."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        item = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(item, Exception):
            raise item

        class _P:
            pass
        p = _P()
        p.stdout = item[0]
        p.returncode = item[1]
        return p


GEN_MODELS_JSON = b'{"data":[{"id":"qwen3.8-27b"}]}'


def test_probe_generation_empty_url_is_liveness_only():
    assert watchdogd.probe_generation(
        {"watchdog_health_url": ""}, runner=_FakeGenRunner([])) is True


def test_gen_base_url_strips_path():
    assert watchdogd._gen_base_url("http://127.0.0.1:8000/health") \
        == "http://127.0.0.1:8000"
    assert watchdogd._gen_base_url("http://127.0.0.1:8000") \
        == "http://127.0.0.1:8000"
    assert watchdogd._gen_base_url("http://127.0.0.1:8000/health/") \
        == "http://127.0.0.1:8000"


def test_probe_generation_success():
    r = _FakeGenRunner([(GEN_MODELS_JSON, 0), (b"200", 0)])
    ok = watchdogd.probe_generation(
        {"watchdog_health_url": "http://127.0.0.1:8000/health"}, runner=r)
    assert ok is True
    # step 1: GET {base}/v1/models (the model name is asked from the
    # server, never config); step 2: POST {base}/v1/chat/completions
    assert r.calls[0][0][-1] == "http://127.0.0.1:8000/v1/models"
    post = r.calls[1][0]
    assert post[post.index("POST") + 1] \
        == "http://127.0.0.1:8000/v1/chat/completions"
    assert "-X" in post and "POST" in post
    assert "-w" in post and "%{http_code}" in post
    payload = json.loads(post[post.index("-d") + 1])
    assert payload == {"model": "qwen3.8-27b", "max_tokens": 5,
                       "messages": [{"role": "user", "content": "ping"}]}


def test_probe_generation_non_200_is_down():
    r = _FakeGenRunner([(GEN_MODELS_JSON, 0), (b"503", 0)])
    assert watchdogd.probe_generation(
        {"watchdog_health_url": "http://127.0.0.1:8000/health"},
        runner=r) is False


def test_probe_generation_models_failure_is_down():
    # a failed GET means generation cannot be verified (the server is
    # exactly what is in question) — the POST is never attempted
    r = _FakeGenRunner([(b"", 7)])  # rc 7 = connection refused
    assert watchdogd.probe_generation(
        {"watchdog_health_url": "http://127.0.0.1:8000/health"},
        runner=r) is False
    assert len(r.calls) == 1


def test_probe_generation_malformed_models_is_down():
    r = _FakeGenRunner([(b"not json", 0), (b"200", 0)])
    assert watchdogd.probe_generation(
        {"watchdog_health_url": "http://127.0.0.1:8000/health"},
        runner=r) is False


def test_probe_generation_post_timeout_is_down():
    r = _FakeGenRunner([(GEN_MODELS_JSON, 0),
                        (subprocess.TimeoutExpired(cmd="curl",
                                                   timeout=180))])
    assert watchdogd.probe_generation(
        {"watchdog_health_url": "http://127.0.0.1:8000/health",
         "watchdog_gen_probe_timeout_s": 180},
        runner=r) is False


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
    # Phase 19: force-cancel Escape, then the ENRICHED text (50 dk).
    assert io.sends == [("cc-proj-a-0a9b", "Escape"),
                        ("cc-proj-a-0a9b", _turn_dead_text(50), "Enter")]
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
    # the recovery pass SKIPS this one (last_ts 7000 is >45 min stale at
    # 10_300 — the normal loop owns it); the loop's nudge is the plain
    # form with the 2nd tick's idle minutes (55 dk).
    assert io.sends == [("cc-proj-a-0a9b", "Escape"),
                        ("cc-proj-a-0a9b", _turn_dead_text(55), "Enter")]
    assert ctx.episodes[("proj-a", 1)].waiting_health is True


# --- tick: Phase 15 outage recovery (dgx_up nudge) ----------------------------
#
# A stuck mid-turn (API error, no stop) keeps turn_active=True and, for a
# <stale-minute outage, a FRESH last_ts. classify() row 1 returns ALIVE for it
# (fresh ledger) before any pane check, so the normal nudge path never runs and
# the session never recovers when the DGX comes back. The recovery pass below
# nudges it on the DOWN->UP transition. Regression: 2026-09-26 outage, 044 sat
# ALIVE through the 22:14 dgx_up tick until a manual user_prompt.


def test_tick_dgx_up_nudges_stuck_midturn_fresh_ledger(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=False)
    sdir = _mk(turn_active=True)  # mid-turn when the outage hit
    ledger.append(sdir, {"type": "user_prompt", "ts": 9_400.0})  # 10 min < 45
    ctx = _ctx(io, now=10_000.0, down=True)
    assert ctx.health.status == "DOWN"
    line = watchdogd.tick(ctx)
    # during the outage it is masked ALIVE (fresh ledger) -> no nudge (bug)
    assert line["sessions"][0]["outcome"] == "ALIVE"
    assert io.sends == []

    io.ok = True  # DGX back -> one good probe -> DOWN->UP transition
    ctx.now = 10_900.0  # 15-min outage; 9_400 still < 45 min (fresh)
    line2 = watchdogd.tick(ctx)
    assert "dgx_up" in line2["events"]
    # Phase 19: ESC first, then the OUTAGE-form text (the DOWN/UP ts
    # strings are TZ-local — assert structure, not exact ts).
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")
    text = [s for s in io.sends if s[-1] == "Enter"][0][1]
    assert text.startswith("continue\n\n")
    assert "The model server is back online:" in text
    assert "(15 min down)" in text
    assert "check git status and the last output" in text
    assert "dgx_up_recovery:proj-a:001" in line2["events"]
    assert ledger.read(sdir)[-1]["type"] == "watchdog_nudge"


def test_tick_dgx_up_recovery_ignores_idle_and_ineligible(cc_home):
    # Only eligible + turn_active sessions are nudged: an idle (turn_active
    # False) session and a ROTATED one are left alone.
    io = FakeIO(health_ok=False)
    idle = _mk(key="idle-proj", nnn=1, turn_active=False)
    rot = _mk(key="rot-proj", nnn=1, st="ROTATED", turn_active=True)
    ledger.append(idle, {"type": "user_prompt", "ts": 9_400.0})
    ledger.append(rot, {"type": "user_prompt", "ts": 9_400.0})
    ctx = _ctx(io, now=10_000.0, down=True)
    watchdogd.tick(ctx)
    io.ok = True
    ctx.now = 10_900.0
    watchdogd.tick(ctx)
    assert io.sends == []  # neither idle nor ROTATED is nudged


def test_tick_dgx_up_recovery_fires_once_not_every_up_tick(cc_home):
    # Runs only on the DOWN->UP transition tick, so a recovery turn in flight
    # (still turn_active) is not re-nudged on a later, non-transition UP tick.
    io = FakeIO(health_ok=False)
    sdir = _mk(turn_active=True)
    ledger.append(sdir, {"type": "user_prompt", "ts": 9_400.0})
    ctx = _ctx(io, now=10_000.0, down=True)
    watchdogd.tick(ctx)
    io.ok = True
    ctx.now = 10_900.0
    watchdogd.tick(ctx)  # transition tick -> one nudge
    assert len(io.sends) == 2  # Phase 19: ESC + text+Enter
    ctx.now = 11_000.0  # still UP, NOT a transition -> no re-nudge
    watchdogd.tick(ctx)
    assert len(io.sends) == 2


# --- tick: nudge restriction to the most-recently-active session ---------------
#
# The DGX model is a single shared resource; reviving many sessions at once
# (the per-session TURN_DEAD/STALLED nudge and the Phase 15 dgx_up recovery
# both walk every eligible session) overloads it. When
# ``watchdog_nudge_only_most_recent`` is on, at most ONE session may be
# nudged per tick: the most-recently-active eligible one (max last_ledger_ts —
# the same "most recently active" definition the digest relay uses to pick its
# injection target). The others stay dormant (their nudge counter does not
# advance). Config off = existing behavior (every eligible session may be
# nudged), kept as the default for back-compat.


def test_tick_only_most_recent_nudged_when_config_on(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    a = _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    b = _mk(key="proj-b", nnn=1, turn_active=True, tmux="cc-proj-b-0b9c")
    # both TURN_DEAD (stale ledger); a is the more-recent of the two.
    ledger.append(a, {"type": "user_prompt", "ts": 10_000.0 - 50 * 60})
    ledger.append(b, {"type": "user_prompt", "ts": 10_000.0 - 100 * 60})
    ctx = _ctx(io, cfg_over={"watchdog_nudge_only_most_recent": True},
               now=10_000.0)
    line = watchdogd.tick(ctx)
    by = {(s["key"], s["nnn"]): s["outcome"] for s in line["sessions"]}
    assert by[("proj-a", 1)] == "TURN_DEAD"
    assert by[("proj-b", 1)] == "TURN_DEAD"
    # only the most-recent eligible (a) is nudged; b stays dormant.
    assert io.sends == [("cc-proj-a-0a9b", "Escape"),
                        ("cc-proj-a-0a9b", _turn_dead_text(50), "Enter")]


def test_tick_focus_healthy_most_recent_preempts_dead_older(cc_home, monkeypatch):
    # The restriction is "only the most recent may progress", not "nudge the
    # most recent of the dead": if the most-recent eligible session is ALIVE,
    # an older TURN_DEAD one is NOT the focus and must not be nudged.
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    alive = _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    dead = _mk(key="proj-b", nnn=1, turn_active=True, tmux="cc-proj-b-0b9c")
    ledger.append(alive, {"type": "user_prompt", "ts": 9_900.0})  # fresh -> ALIVE
    ledger.append(dead, {"type": "user_prompt", "ts": 10_000.0 - 100 * 60})
    ctx = _ctx(io, cfg_over={"watchdog_nudge_only_most_recent": True},
               now=10_000.0)
    line = watchdogd.tick(ctx)
    by = {(s["key"], s["nnn"]): s["outcome"] for s in line["sessions"]}
    assert by[("proj-a", 1)] == "ALIVE"
    assert by[("proj-b", 1)] == "TURN_DEAD"
    assert io.sends == []  # dead session is not the focus -> not nudged


def test_tick_turn_dead_nudges_all_when_config_off(cc_home, monkeypatch):
    # Back-compat: with the key off (the default), every TURN_DEAD eligible
    # session is nudged, as before.
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    a = _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    b = _mk(key="proj-b", nnn=1, turn_active=True, tmux="cc-proj-b-0b9c")
    ledger.append(a, {"type": "user_prompt", "ts": 10_000.0 - 50 * 60})
    ledger.append(b, {"type": "user_prompt", "ts": 10_000.0 - 100 * 60})
    ctx = _ctx(io, now=10_000.0)  # key not set -> off
    watchdogd.tick(ctx)
    assert {s[0] for s in io.sends} == {"cc-proj-a-0a9b", "cc-proj-b-0b9c"}
    # Phase 19: ESC + text+Enter per session
    assert len(io.sends) == 4


def test_tick_dgx_up_recovery_only_most_recent_when_config_on(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=False)
    a = _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    b = _mk(key="proj-b", nnn=1, turn_active=True, tmux="cc-proj-b-0b9c")
    # both stuck mid-turn with fresh ledgers; a is the more-recent of the two.
    ledger.append(a, {"type": "user_prompt", "ts": 9_500.0})
    ledger.append(b, {"type": "user_prompt", "ts": 9_000.0})
    ctx = _ctx(io, cfg_over={"watchdog_nudge_only_most_recent": True},
               now=10_000.0, down=True)
    watchdogd.tick(ctx)  # during the outage: masked ALIVE -> no nudge
    io.ok = True
    ctx.now = 10_900.0
    line2 = watchdogd.tick(ctx)  # DOWN->UP transition
    assert "dgx_up" in line2["events"]
    # Phase 19: ESC first, then the OUTAGE-form text (the DOWN/UP ts
    # strings are TZ-local — assert structure, not exact ts).
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")
    text = [s for s in io.sends if s[-1] == "Enter"][0][1]
    assert text.startswith("continue\n\n")
    assert "The model server is back online:" in text
    kapaliydi = "kapal" + chr(0x131) + "yd" + chr(0x131)  # byte-safe (dotless i)
    assert f"(15 dk {kapaliydi})" in text
    assert len([s for s in io.sends if s[-1] == "Enter"]) == 1  # only a
    assert "dgx_up_recovery:proj-a:001" in line2["events"]
    assert not any(e.startswith("dgx_up_recovery:proj-b")
                   for e in line2["events"])


def test_tick_dgx_up_recovery_nudges_all_when_config_off(cc_home, monkeypatch):
    # Back-compat for Phase 15: with the key off, every stuck eligible
    # session is recovered on dgx_up, as before.
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=False)
    a = _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    b = _mk(key="proj-b", nnn=1, turn_active=True, tmux="cc-proj-b-0b9c")
    ledger.append(a, {"type": "user_prompt", "ts": 9_500.0})
    ledger.append(b, {"type": "user_prompt", "ts": 9_000.0})
    ctx = _ctx(io, now=10_000.0, down=True)  # key not set -> off
    watchdogd.tick(ctx)
    io.ok = True
    ctx.now = 10_900.0
    watchdogd.tick(ctx)
    assert {s[0] for s in io.sends} == {"cc-proj-a-0a9b", "cc-proj-b-0b9c"}
    # Phase 19: ESC + text+Enter per session, all OUTAGE-form.
    assert len(io.sends) == 4
    kapaliydi = "kapal" + chr(0x131) + "yd" + chr(0x131)  # byte-safe (dotless i)
    assert all(f"(15 dk {kapaliydi})" in s[1]
               for s in io.sends if s[-1] == "Enter")


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
    t1 = _turn_dead_text(50)
    watchdogd.tick(ctx)  # NUDGE: ESC + text + Enter; the Enter was lost…
    assert io.sends == [("cc-proj-a-0a9b", "Escape"),
                        ("cc-proj-a-0a9b", t1, "Enter")]
    # …and the MULTI-LINE text now sits in the input box (the 11:30 case).
    io.pane = "some output line\n❯ continue\n" + t1.split("\n\n", 1)[1]
    ctx.now = 10_300.0
    l2 = watchdogd.tick(ctx)
    assert l2["sessions"][0]["outcome"] == "ALIVE"  # capture CHANGED
    assert io.sends[-1] == ("cc-proj-a-0a9b", t1, "Enter")  # no-op
    ctx.now = 10_600.0
    l3 = watchdogd.tick(ctx)  # now UNCHANGED → WAIT (multiline input)
    assert l3["sessions"][0]["outcome"] == "WAIT"
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
    # Phase 19: tick 1 ESC + T50, ticks 2-3 plain RESEND T55/T60, tick 4 []
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")
    assert io.sends[1] == ("cc-proj-a-0a9b", _turn_dead_text(50), "Enter")
    assert io.sends[2] == ("cc-proj-a-0a9b", _turn_dead_text(55), "Enter")
    assert io.sends[3] == ("cc-proj-a-0a9b", _turn_dead_text(60), "Enter")
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
    # Phase 22-C: no turn in flight (turn_active=False) → the PICKER
    # path: ONE force-cancel Esc + ONE frozen_nudge push, no nudge
    # (deliberate behavior change from the announce-only Phase 10)
    assert [p[0] for p in pushes] == ["freeze_detected", "frozen_nudge"]
    assert pushes[1][3]["frozen_minutes"] == 10  # ticks=2 → 2×300//60
    assert io.sends == [("cc-proj-a-0a9b", "Escape")]
    assert ledger.read(paths.session_dir("proj-a", 1))[-1]["type"] \
        == "watchdog_freeze"
    l4 = watchdogd.tick(ctx)  # still FROZEN — the Esc/push fires ONCE
    assert l4["sessions"][0]["outcome"] == "FROZEN"
    assert [p[0] for p in pushes] == ["freeze_detected", "frozen_nudge"]
    assert io.sends == [("cc-proj-a-0a9b", "Escape")]


def test_tick_frozen_dedup_seeded_from_ledger(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE,
                sessions=["cc-proj-a-0a9b"])
    sdir = _mk(turn_active=False)
    _stale_prompt(sdir, now=10_000.0)
    ledger.append(sdir, {"type": "watchdog_freeze"})  # pushed pre-restart
    ctx = _ctx(io, now=10_000.0)
    ctx.froze = watchdogd._seed_froze(io.list_sessions)
    # Phase 22: per-TARGET (the pane), not (key, nnn)
    assert "cc-proj-a-0a9b" in ctx.froze
    for _ in range(3):
        watchdogd.tick(ctx)
    # the seeded announce is deduped (no freeze_detected re-push after
    # the daemon restart), but the picker path still sends its ONE
    # Esc + frozen_nudge push (a SEPARATE in-memory dedup: frozen_esc)
    assert [p[0] for p in pushes] == ["frozen_nudge"]
    assert io.sends == [("cc-proj-a-0a9b", "Escape")]


def test_tick_frozen_turn_escalates_after_threshold(cc_home, monkeypatch):
    # Phase 22-C: a FROZEN pane WITH a turn in flight escalates once
    # the freeze outlives watchdog_frozen_nudge_minutes (default 20 min
    # = 1200s at 300s ticks): ESC force-cancel + the frozen-form nudge
    # + the frozen_nudge push, THEN the generation probe (ONLY on
    # suspicion — the run's first probe is the escalation tick)
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE)
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    l1 = watchdogd.tick(ctx)  # WAIT
    assert l1["sessions"][0]["outcome"] == "WAIT"
    l2 = watchdogd.tick(ctx)  # WAIT
    assert l2["sessions"][0]["outcome"] == "WAIT"
    l3 = watchdogd.tick(ctx)  # FROZEN (ticks=2): 600s < 1200s → not due
    assert l3["sessions"][0]["outcome"] == "FROZEN"
    assert [p[0] for p in pushes] == ["freeze_detected"]
    assert io.sends == []
    assert io.gen_probe_calls == 0  # no suspicion yet → no probe
    l4 = watchdogd.tick(ctx)  # ticks=3: 900s < 1200s → still not due
    assert l4["sessions"][0]["outcome"] == "FROZEN"
    assert [p[0] for p in pushes] == ["freeze_detected"]
    assert io.sends == []
    l5 = watchdogd.tick(ctx)  # ticks=4: 1200s >= 1200s → escalate
    assert l5["sessions"][0]["outcome"] == "FROZEN"
    frozen_text = watchdog.build_nudge_text("continue", now=10_000.0,
                                            frozen_minutes=20)
    assert io.sends == [("cc-proj-a-0a9b", "Escape"),
                        ("cc-proj-a-0a9b", frozen_text, "Enter")]
    assert [p[0] for p in pushes] == ["freeze_detected", "frozen_nudge"]
    ev, slug, nnn, fields = pushes[1]
    assert (slug, nnn) == ("proj-a", 1)
    assert fields["frozen_minutes"] == 20
    assert fields["nudge"] == 1
    assert fields["max_nudges"] == 3
    assert io.gen_probe_calls == 1  # ONLY the escalation tick
    assert ledger.read(paths.session_dir("proj-a", 1))[-1]["type"] \
        == "watchdog_nudge"


def test_tick_frozen_multi_record_same_pane_escalates_once(cc_home,
                                                            monkeypatch):
    # Phase 22-C: stale records may resolve to the SAME pane — the
    # announce is deduped per PANE (one freeze_detected), and the
    # escalation per pane per tick (the tick-local escalated set: one
    # nudge, one push, one gen probe for the whole pane)
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE,
                sessions=["cc-proj-a-0a9b"])
    _mk(key="proj-a", nnn=1, turn_active=True, tmux="cc-proj-a-0a9b")
    _mk(key="proj-a", nnn=2, turn_active=True, tmux="cc-proj-a-0a9b")
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    _stale_prompt(paths.session_dir("proj-a", 2), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    for _ in range(2):
        watchdogd.tick(ctx)  # both WAIT
    l3 = watchdogd.tick(ctx)  # both FROZEN → ONE freeze push (per pane)
    assert [l["outcome"] for l in l3["sessions"]] == ["FROZEN", "FROZEN"]
    assert [p[0] for p in pushes] == ["freeze_detected"]
    watchdogd.tick(ctx)  # ticks=3: 900s < 1200s → not due
    assert [p[0] for p in pushes] == ["freeze_detected"]
    l5 = watchdogd.tick(ctx)  # ticks=4: due → ONE escalation
    assert [p[0] for p in pushes] == ["freeze_detected", "frozen_nudge"]
    assert io.gen_probe_calls == 1
    assert len([s for s in io.sends if s[-1] == "Enter"]) == 1
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")


def test_tick_frozen_eleven_records_same_pane_escalates_once(cc_home,
                                                              monkeypatch):
    # Phase 22-D: the 2-record dedup variant at the LIVE scale — the
    # real 2026-09-29 incident left 11 stale records (rotated-out
    # connector sessions) that all resolve to the SAME pane. The
    # per-PANE announce (ctx.froze) must collapse them to ONE
    # freeze_detected, and the tick-local escalated set must collapse
    # the escalation to ONE nudge / ONE frozen_nudge push / ONE gen
    # probe for the whole pane — not 11 of each.
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE,
                sessions=["cc-proj-a-0a9b"])
    for nnn in range(1, 12):  # 11 live records, one pane
        _mk(key="proj-a", nnn=nnn, turn_active=True,
            tmux="cc-proj-a-0a9b")
        _stale_prompt(paths.session_dir("proj-a", nnn), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    for _ in range(2):
        watchdogd.tick(ctx)  # all 11 WAIT
    l3 = watchdogd.tick(ctx)  # all 11 FROZEN → ONE freeze push (per pane)
    assert [l["outcome"] for l in l3["sessions"]] == ["FROZEN"] * 11
    assert [p[0] for p in pushes] == ["freeze_detected"]
    watchdogd.tick(ctx)  # ticks=3: 900s < 1200s → not due
    assert [p[0] for p in pushes] == ["freeze_detected"]
    l5 = watchdogd.tick(ctx)  # ticks=4: due → ONE escalation
    assert [p[0] for p in pushes] == ["freeze_detected", "frozen_nudge"]
    assert io.gen_probe_calls == 1
    assert len([s for s in io.sends if s[-1] == "Enter"]) == 1
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")
    # exactly one freeze announce event across all 11 records
    assert sum(1 for e in l5["events"] if e.startswith("freeze:")) == 0
    assert sum(1 for e in l3["events"] if e.startswith("freeze:")) == 1


def test_tick_frozen_gen_down_power_cycles(cc_home, monkeypatch):
    # Phase 22-C Wave 4: the generation probe (ONLY on suspicion) goes
    # DOWN (2-fail debounce) while LIVENESS stays UP — a hung vLLM
    # still serves /health, so the per-tick liveness probe can never
    # see it. Sustained gen-DOWN past power_cycle_after_s (default
    # 600s) triggers the SHARED power cycle (the same relay and the
    # same cooldown file as the liveness cycle — one relay, one
    # 30-min window).
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE,
                gen_ok=[False, False, False])
    io.cycle_result = {"ok": True, "state": "cycled"}
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, cfg_over={"power_plug_ip": "192.0.2.34"},
               now=10_000.0)
    watchdogd.tick(ctx)  # WAIT
    ctx.now = 10_600.0
    watchdogd.tick(ctx)  # WAIT
    ctx.now = 11_200.0
    l3 = watchdogd.tick(ctx)  # FROZEN announce; 600s < 1200s → not due
    assert l3["sessions"][0]["outcome"] == "FROZEN"
    ctx.now = 11_800.0
    watchdogd.tick(ctx)  # ticks=3: 900s < 1200s → not due
    ctx.now = 12_400.0
    l5 = watchdogd.tick(ctx)  # escalate: NUDGE + gen fail 1 (debounce)
    assert io.gen_probe_calls == 1
    assert ctx.gen.status == "UNKNOWN"
    assert "gen_down" not in l5["events"]
    assert io.cycle_calls == 0
    ctx.now = 13_000.0
    l6 = watchdogd.tick(ctx)  # RESEND + gen fail 2 → DOWN (down_since=now)
    assert ctx.gen.status == "DOWN"
    assert "gen_down" in l6["events"]
    assert io.cycle_calls == 0  # down age 0 < 600s
    ctx.now = 13_600.0
    l7 = watchdogd.tick(ctx)  # RESEND + gen fail 3 → age 600s ≥ 600s
    assert io.cycle_calls == 1
    assert l7["power_cycle"]["ok"] is True
    assert any(p[0] == "dgx_power_cycle" for p in pushes)
    assert io.recorded_cycle == [13_600.0]


def test_tick_frozen_gen_up_recovers(cc_home, monkeypatch):
    # Phase 22-C Wave 4: gen DOWN (2 fails) → UP (ok again): the dgx_up
    # push + _recover_stuck_turns (the existing outage recovery) fire
    # on the GEN transition (liveness was never DOWN). The FROZEN
    # session itself must NOT be double-nudged by the recovery pass
    # (its last_ts is stale → the normal loop owns it).
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True, pane=BUSY_PANE,
                gen_ok=[False, False, True])
    _mk(turn_active=True)
    _stale_prompt(paths.session_dir("proj-a", 1), now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    watchdogd.tick(ctx)  # WAIT
    ctx.now = 10_600.0
    watchdogd.tick(ctx)  # WAIT
    ctx.now = 11_200.0
    watchdogd.tick(ctx)  # FROZEN announce; 600s < 1200s → not due
    ctx.now = 11_800.0
    watchdogd.tick(ctx)  # ticks=3: not due
    ctx.now = 12_400.0
    l5 = watchdogd.tick(ctx)  # escalate: NUDGE + gen fail 1
    assert io.gen_probe_calls == 1
    assert ctx.gen.status == "UNKNOWN"
    ctx.now = 13_000.0
    l6 = watchdogd.tick(ctx)  # RESEND + gen fail 2 → DOWN
    assert ctx.gen.status == "DOWN"
    ctx.now = 13_600.0
    l7 = watchdogd.tick(ctx)  # RESEND + gen ok → UP
    assert "dgx_up" in l7["events"]
    dgu = [p for p in pushes if p[0] == "dgx_up"][0]
    assert dgu[3]["downtime_s"] == 600.0  # 13_600 - down_since 13_000
    assert io.gen_probe_calls == 3
    # the recovery pass did NOT nudge the FROZEN session (stale
    # last_ts): no OUTAGE-form nudge text was ever sent
    assert not any("Model sunucusu" in s[1] for s in io.sends
                   if len(s) > 1)


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


# --- Phase 19: ESC force-cancel + nudge enrichment ----------------------------


def test_tick_nudge_esc_precedes_text_and_settles(cc_home, monkeypatch):
    # the first physical nudge: force-cancel Escape, settle, THEN text
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    watchdogd.tick(ctx)
    t = "cc-proj-a-0a9b"
    assert io.sends[0] == (t, "Escape")
    assert io.slept == [0.75]
    assert io.sends[1] == (t, _turn_dead_text(50), "Enter")


def test_tick_resend_does_not_escape(cc_home, monkeypatch):
    # a RESEND (lost Enter, second attempt) must NOT ESC — the pane is
    # at the prompt; an ESC would risk cancelling what is typed next.
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    _stale_prompt(sdir, now=10_000.0)
    ctx = _ctx(io, now=10_000.0)
    watchdogd.tick(ctx)  # NUDGE: ESC + text + Enter
    assert io.sends[0] == ("cc-proj-a-0a9b", "Escape")
    assert io.slept == [0.75]
    io.sends.clear()
    io.slept.clear()
    ctx.now = 10_300.0
    watchdogd.tick(ctx)  # RESEND: text + Enter only
    assert io.sends == [("cc-proj-a-0a9b", _turn_dead_text(55), "Enter")]
    assert io.slept == []


def test_tick_dgx_up_nudge_text_includes_power_cycle_when_in_window(
        cc_home, monkeypatch):
    # the dgx_up nudge names the power_cycle ONLY when its ts falls
    # inside the outage window
    _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=False)
    sdir = _mk(turn_active=True)
    ledger.append(sdir, {"type": "user_prompt", "ts": 9_400.0})
    io.last_cycle_ts_value = 10_500.0  # inside 10_000 -> 10_900
    ctx = _ctx(io, now=10_000.0, down=True)
    watchdogd.tick(ctx)
    io.ok = True
    ctx.now = 10_900.0
    watchdogd.tick(ctx)
    text = [s for s in io.sends if s[-1] == "Enter"][0][1]
    assert chr(0x2192) + " power_cycle " in text
    kapaliydi = "kapal" + chr(0x131) + "yd" + chr(0x131)  # byte-safe (dotless i)
    assert f"(15 dk {kapaliydi})" in text

    # a cycle ts BEFORE the outage window must not appear
    io2 = FakeIO(health_ok=False)
    sdir2 = _mk(key="proj-b", nnn=1, turn_active=True,
                tmux="cc-proj-b-0b9c")
    ledger.append(sdir2, {"type": "user_prompt", "ts": 9_400.0})
    io2.last_cycle_ts_value = 100.0
    ctx2 = _ctx(io2, now=10_000.0, down=True)
    watchdogd.tick(ctx2)
    io2.ok = True
    ctx2.now = 10_900.0
    watchdogd.tick(ctx2)
    text2 = [s for s in io2.sends if s[-1] == "Enter"][0][1]
    assert "power_cycle" not in text2
    assert f"(15 dk {kapaliydi})" in text2


def test_tick_pending_multiline_nudge_submitted_via_send_enter(
        cc_home, monkeypatch):
    # daemon restart: the episode is GONE (in-memory) but the ledger
    # holds the sent text and it sits in the input box. Fresh episode
    # + pending -> a bare Enter (SEND_ENTER): no ESC, no re-paste.
    # (sent/pane are DERIVED from build_nudge_text — never retyped.)
    pushes = _capture_pushes(monkeypatch)
    io = FakeIO(health_ok=True)
    sdir = _mk(turn_active=True)
    ledger.append(sdir, {"type": "user_prompt", "ts": 1_000.0})  # stale
    sent = watchdog.build_nudge_text("continue", now=10_900.0,
                                     down_since=10_000.0)
    ledger.append(sdir, {"type": "watchdog_nudge", "n": 1,
                         "text": sent, "ts": 9_000.0})
    io.pane = "some output line\n❯ continue\n" + sent.split("\n\n", 1)[1]
    ctx = _ctx(io, now=10_000.0)
    line = watchdogd.tick(ctx)
    assert line["sessions"][0]["outcome"] == "WAIT"
    assert io.sends == [("cc-proj-a-0a9b", "Enter")]  # bare Enter only
    assert ctx.episodes[("proj-a", 1)].n == 0  # SEND_ENTER not counted
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


def test_plist_render_bakes_tmux_bin_when_given(cc_home, tmp_path):
    # launchd's minimal PATH has no homebrew, so a bare `tmux` on PATH
    # never resolves there — the daemon logged `tmux_unavailable` every
    # tick and could not see a single session. Bake the absolute tmux
    # path in at install time (same pattern as the binary path).
    xml = watchdogd.plist_render("/usr/local/bin/cc-continuity",
                                 tmp_path / "home",
                                 tmux_bin="/opt/homebrew/bin/tmux")
    assert "<key>CC_CONTINUITY_TMUX_BIN</key>" in xml
    assert "<string>/opt/homebrew/bin/tmux</string>" in xml
    xml_none = watchdogd.plist_render("/usr/local/bin/cc-continuity",
                                      tmp_path / "home")
    assert "CC_CONTINUITY_TMUX_BIN" not in xml_none


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


# --- auto power-cycle (Phase 13 design 7899) ---------------------------------


class _P:
    def __init__(self, code, out):
        self.returncode = code
        self.stdout = out
        self.stderr = b""


def test_cycle_power_posts_to_endpoint_and_parses_json():
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return _P(0, b'{"ok": true, "state": "on"}')

    res = watchdogd.cycle_power({}, runner=fake)
    assert res == {"ok": True, "state": "on"}
    assert "-X" in calls[0] and "POST" in calls[0]
    assert any("/cycle" in c for c in calls[0])


def test_cycle_power_endpoint_error_json_kept():
    res = watchdogd.cycle_power(
        {}, runner=lambda cmd, **kw: _P(
            0, b'{"ok": false, "state": "off", "error": "relay stuck"}'))
    assert res["ok"] is False
    assert res["state"] == "off"
    assert res["error"] == "relay stuck"


def test_cycle_power_runner_raises_degrades():
    def fake(cmd, **kw):
        raise OSError("boom")

    res = watchdogd.cycle_power({}, runner=fake)
    assert res["ok"] is False
    assert res["state"] == "unknown"


def test_cycle_power_non_json_degrades():
    res = watchdogd.cycle_power({}, runner=lambda cmd, **kw: _P(0, b"nope"))
    assert res["ok"] is False
    assert res["state"] == "unknown"


def test_last_cycle_ts_roundtrip(cc_home):
    assert watchdogd.load_last_cycle_ts() is None
    watchdogd.save_last_cycle_ts(1234.5)
    assert watchdogd.load_last_cycle_ts() == 1234.5


def test_last_cycle_ts_corrupt_file_is_none(cc_home):
    (paths.runtime_dir() / "power-cycle.json").write_text(
        "{not json", encoding="utf-8")
    assert watchdogd.load_last_cycle_ts() is None


def _down_ctx(io, cfg_over, now=10_000.0, down_age_s=600.0):
    """A ctx whose health went DOWN ``down_age_s`` seconds before now."""
    ctx = _ctx(io, cfg_over=cfg_over, now=now)
    ctx.health.feed(False, now - down_age_s - 100)
    ctx.health.feed(False, now - down_age_s)  # 2nd fail -> DOWN at now-age
    return ctx


def test_tick_power_cycle_after_sustained_down(cc_home, monkeypatch):
    io = FakeIO(health_ok=False)
    io.cycle_result = {"ok": True, "state": "on"}
    pushes = _capture_pushes(monkeypatch)
    now = 10_000.0
    ctx = _down_ctx(io, {"power_plug_ip": "192.0.2.34"}, now=now)
    line = watchdogd.tick(ctx)
    assert io.cycle_calls == 1
    assert any(ev == "dgx_power_cycle" for ev, *_ in pushes)
    assert line["power_cycle"]["ok"] is True
    assert "power_cycle" in line["events"]
    assert io.recorded_cycle == [now]


def test_tick_no_power_cycle_before_threshold(cc_home, monkeypatch):
    io = FakeIO(health_ok=False)
    io.cycle_result = {"ok": True, "state": "on"}
    _capture_pushes(monkeypatch)
    now = 10_000.0
    ctx = _down_ctx(io, {"power_plug_ip": "192.0.2.34"},
                    now=now, down_age_s=100.0)  # 100s < 600s
    line = watchdogd.tick(ctx)
    assert io.cycle_calls == 0
    assert "power_cycle" not in line


def test_tick_power_cycle_blocked_by_cooldown(cc_home, monkeypatch):
    io = FakeIO(health_ok=False)
    io.cycle_result = {"ok": True, "state": "on"}
    io.last_cycle_ts_value = 10_000.0 - 1200.0  # 20 min < 30 min cooldown
    _capture_pushes(monkeypatch)
    now = 10_000.0
    ctx = _down_ctx(io, {"power_plug_ip": "192.0.2.34"}, now=now)
    watchdogd.tick(ctx)
    assert io.cycle_calls == 0
    assert io.recorded_cycle == []


def test_tick_power_cycle_failed_does_not_record_cooldown(
        cc_home, monkeypatch):
    io = FakeIO(health_ok=False)
    io.cycle_result = {"ok": False, "state": "off",
                       "error": "plug unreachable"}
    pushes = _capture_pushes(monkeypatch)
    now = 10_000.0
    ctx = _down_ctx(io, {"power_plug_ip": "192.0.2.34"}, now=now)
    line = watchdogd.tick(ctx)
    assert io.cycle_calls == 1  # retried on the next tick, not cooldown
    assert io.recorded_cycle == []
    assert any(e.startswith("power_cycle_failed")
               for e in line["events"])
    assert not any(ev == "dgx_power_cycle" for ev, *_ in pushes)


def test_tick_no_power_cycle_without_plug_ip(cc_home, monkeypatch):
    io = FakeIO(health_ok=False)
    io.cycle_result = {"ok": True, "state": "on"}
    _capture_pushes(monkeypatch)
    now = 10_000.0
    ctx = _down_ctx(io, {}, now=now)  # no power_plug_ip in cfg
    watchdogd.tick(ctx)
    assert io.cycle_calls == 0


# --- Phase 21: exit-hang daemon I/O --------------------------------------------
#
# /exit in CC 2.1.209 can hang: the marker entry is written but the process
# never dies. The 5s fast pass (check_exit_hangs) finds the hung pane's
# topmost claude process via the pane process tree + the transcript marker and
# kills it after the grace (default 30s; CC_CONTINUITY_EXIT_HANG_LIMIT).
# No-op passes write ZERO log lines (the 5s cadence must not bloat the log).

EXIT_MARK = "<command-name>/exit</command-name>"
_PANE = {"pane": "%23", "pid": 24817, "path": "/Users/devx/proj"}
_PROCMAP = {24817: (1, "/bin/sh /usr/bin/command claude -n old"),
            24822: (24817, "claude --dangerously-skip-permissions -n old")}


def _exit_entries(now=10_000.0, age=40.0, after=()):
    m = now - age
    return [(m, "user", EXIT_MARK + "  "),
            (m, "user",
             "<local-command-stdout>Bye!</local-command-stdout>"),
            (m + 7, "assistant", "No response requested.")] + list(after)


def _hung_io(**over):
    kw = dict(health_ok=True, sessions=["cc-proj-a-0a9b"],
              panes={"cc-proj-a-0a9b": [_PANE]}, procmap=_PROCMAP,
              transcript=_exit_entries(), alive_pids={24822})
    kw.update(over)
    return FakeIO(**kw)


def _exit_ctx(io, cfg_over=None, now=10_000.0):
    cfg = {"watchdog_enabled": True, "watchdog_interval_s": 300}
    cfg.update(cfg_over or {})
    return TickCtx(cfg=cfg, health=HealthState(), episodes={},
                   prev_captures={}, froze=set(), probe=io.probe,
                   capture=io.capture, list_sessions=io.list_sessions,
                   send=io.send, now=now,
                   list_panes=io.list_panes, ps_snapshot=io.ps_snapshot,
                   read_transcript=io.read_transcript,
                   is_alive=io.is_alive, kill=io.kill,
                   kill_session=io.kill_session,
                   process_start_ts=io.process_start_ts)


# --- exit_hang_limit_s -----------------------------------------------------------

def test_exit_hang_limit_s_default(monkeypatch):
    monkeypatch.delenv("CC_CONTINUITY_EXIT_HANG_LIMIT", raising=False)
    assert watchdogd.exit_hang_limit_s() == 30.0


def test_exit_hang_limit_s_env_override(monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_EXIT_HANG_LIMIT", "45")
    assert watchdogd.exit_hang_limit_s() == 45.0


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5", ""])
def test_exit_hang_limit_s_broken_env_falls_back(monkeypatch, bad):
    # a 0/negative grace would kill a mid-/exit — reject like a typo
    monkeypatch.setenv("CC_CONTINUITY_EXIT_HANG_LIMIT", bad)
    assert watchdogd.exit_hang_limit_s() == 30.0


# --- kill_process ------------------------------------------------------------------

def _fake_clock(start=100.0):
    t = [start]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s
    return clock, sleep


class _FakeOsKill:
    """Stands in for os.kill with a death schedule: ``die_on`` is the sig
    that kills the process (None = immortal). Liveness probes (sig 0)
    report the current state."""

    def __init__(self, die_on=None, alive=True):
        self.die_on = die_on
        self.alive = alive
        self.signals = []

    def __call__(self, pid, sig):
        self.signals.append(sig)
        if sig == 0:
            if self.alive:
                return None
            raise ProcessLookupError()
        if sig == self.die_on:
            self.alive = False


def test_kill_process_sigterm_kills():
    fk = _FakeOsKill(die_on=signal.SIGTERM)
    clock, sleep = _fake_clock()
    res = watchdogd.kill_process(4242, kill=fk, clock=clock, sleep=sleep)
    assert res == "term"
    assert signal.SIGKILL not in fk.signals


def test_kill_process_already_dead_sends_nothing():
    fk = _FakeOsKill(alive=False)
    clock, sleep = _fake_clock()
    assert watchdogd.kill_process(4242, kill=fk, clock=clock, sleep=sleep) \
        == "dead"
    assert fk.signals == [0]  # the liveness probe only, no signal sent


def test_kill_process_term_ignored_escalates_to_kill():
    fk = _FakeOsKill(die_on=None)  # immortal: survives TERM (and KILL)
    clock, sleep = _fake_clock()
    res = watchdogd.kill_process(4242, term_wait_s=1.0, kill=fk,
                                 clock=clock, sleep=sleep)
    assert res == "kill"
    assert fk.signals[0] == 0
    assert fk.signals[1] == signal.SIGTERM
    assert fk.signals[-1] == signal.SIGKILL
    assert clock() == pytest.approx(101.0)  # the deadline was actually honored


def test_kill_process_never_raises():
    def boom(pid, sig):
        raise OSError(13, "EPERM")

    clock, sleep = _fake_clock()
    # a signal error degrades to "dead" — never a false "killed" push
    assert watchdogd.kill_process(4242, kill=boom, clock=clock,
                                  sleep=sleep) == "dead"


# --- ps_tree ------------------------------------------------------------------------

def test_ps_tree_parses_pid_ppid_command():
    out = b"  12 1 sh -c x\n  34 12 claude -n a\n"
    got = watchdogd.ps_tree(runner=lambda *a, **k: _P(0, out))
    assert got == {12: (1, "sh -c x"), 34: (12, "claude -n a")}


def test_ps_tree_command_with_spaces_kept():
    # the command column is the REST of the line (it may contain spaces)
    out = (b"  34 12 /opt/homebrew/bin/claude "
           b"--dangerously-skip-permissions -n x\n")
    got = watchdogd.ps_tree(runner=lambda *a, **k: _P(0, out))
    assert got == {34: (12, "/opt/homebrew/bin/claude "
                               "--dangerously-skip-permissions -n x")}


def test_ps_tree_malformed_rows_skipped():
    out = b"  34 12 ok\n  garbage\n  99\n"
    got = watchdogd.ps_tree(runner=lambda *a, **k: _P(0, out))
    assert got == {34: (12, "ok")}


def test_ps_tree_failure_is_none():
    assert watchdogd.ps_tree(runner=lambda *a, **k: _P(1, b"")) is None

    def boom(*a, **k):
        raise OSError("no ps")

    assert watchdogd.ps_tree(runner=boom) is None


# --- list_tmux_panes ------------------------------------------------------------------

def test_list_tmux_panes_parses_with_space_paths():
    # pane_current_path may contain spaces -> rsplit from the right
    out = b"%23 24822 /Users/devx/my proj/x\n%30 52486 /tmp\n"
    cmds = []

    def run(cmd, **kw):
        cmds.append(cmd)
        return _P(0, out)

    got = watchdogd.list_tmux_panes("cc-proj-a-0a9b", runner=run)
    assert got == [
        {"pane": "%23", "pid": 24822, "path": "/Users/devx/my proj/x"},
        {"pane": "%30", "pid": 52486, "path": "/tmp"},
    ]
    assert cmds[0][1:4] == ["list-panes", "-t", "cc-proj-a-0a9b"]
    assert cmds[0][4] == "-F"


def test_list_tmux_panes_failure_is_none():
    assert watchdogd.list_tmux_panes("s",
                                     runner=lambda *a, **k: _P(1, b"")) is None


def test_list_tmux_panes_malformed_row_skipped():
    out = b"%23 notapid /x\n%30 52486 /tmp\n"
    got = watchdogd.list_tmux_panes("s", runner=lambda *a, **k: _P(0, out))
    assert got == [{"pane": "%30", "pid": 52486, "path": "/tmp"}]


# --- read_transcript_entries --------------------------------------------------------

def _transcript_root(tmp_path, monkeypatch):
    root = tmp_path / "cc-config"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


def _write_jsonl(root, slug, name, rows, mtime=None):
    d = root / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows),
                 encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_read_transcript_entries_parses_string_and_text_blocks(
        cc_home, monkeypatch, tmp_path):
    root = _transcript_root(tmp_path, monkeypatch)
    _write_jsonl(root, "-Users-devx-proj", "a.jsonl", [
        {"type": "user", "timestamp": "2026-09-28T20:43:17.000000Z",
         "message": {"content": "hi there"}},
        {"type": "assistant", "timestamp": "2026-09-28T20:43:18.000000Z",
         "message": {"content": [
             {"type": "text", "text": "hello"},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
             {"type": "text", "text": "done"}]}},
    ])
    got = watchdogd.read_transcript_entries("/Users/devx/proj")
    assert got is not None
    assert [e[1:] for e in got] == [
        ("user", "hi there"),
        ("assistant", "hello\ndone"),  # tool_use is not a text block
    ]
    from datetime import datetime, timezone
    assert got[0][0] == datetime(2026, 9, 28, 20, 43, 17,
                                 tzinfo=timezone.utc).timestamp()


def test_read_transcript_entries_newest_mtime_wins(cc_home, monkeypatch,
                                                    tmp_path):
    root = _transcript_root(tmp_path, monkeypatch)
    _write_jsonl(root, "-Users-devx-proj", "old.jsonl", [
        {"type": "user", "timestamp": "2026-09-28T20:00:00.000000Z",
         "message": {"content": "old"}}], mtime=1_000.0)
    _write_jsonl(root, "-Users-devx-proj", "new.jsonl", [
        {"type": "user", "timestamp": "2026-09-28T21:00:00.000000Z",
         "message": {"content": "new"}}], mtime=2_000.0)
    got = watchdogd.read_transcript_entries("/Users/devx/proj")
    assert [e[2] for e in got] == ["new"]


def test_read_transcript_entries_missing_dir_is_none(cc_home, monkeypatch,
                                                     tmp_path):
    _transcript_root(tmp_path, monkeypatch)
    assert watchdogd.read_transcript_entries("/Users/devx/elsewhere") is None


def test_read_transcript_entries_skips_bad_rows(cc_home, monkeypatch,
                                                tmp_path):
    root = _transcript_root(tmp_path, monkeypatch)
    d = root / "projects" / "-Users-devx-proj"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text(
        "{not json\n"
        + json.dumps({"type": "user", "message": {"content": "no ts"}}) + "\n"
        + json.dumps({"timestamp": "2026-09-28T20:00:00.000000Z",
                      "message": {"content": "no type"}}) + "\n"
        + json.dumps({"type": "user",
                      "timestamp": "2026-09-28T20:43:17.000000Z",
                      "message": {"content": "ok"}}) + "\n",
        encoding="utf-8")
    got = watchdogd.read_transcript_entries("/Users/devx/proj")
    assert [e[2] for e in got] == ["ok"]


def test_read_transcript_entries_reads_tail_only(cc_home, monkeypatch,
                                                 tmp_path):
    # a hung CC stops writing: the marker sits in the TAIL; the head junk
    # line (600KB > the 512KB tail) must be ignored — its partial first
    # line is dropped, not fatal
    root = _transcript_root(tmp_path, monkeypatch)
    d = root / "projects" / "-Users-devx-proj"
    d.mkdir(parents=True)
    big = "X" * (600 * 1024)
    (d / "a.jsonl").write_text(
        big + "\n"
        + json.dumps({"type": "user",
                      "timestamp": "2026-09-28T20:43:17.000000Z",
                      "message": {"content": EXIT_MARK + "  "}}) + "\n",
        encoding="utf-8")
    got = watchdogd.read_transcript_entries("/Users/devx/proj")
    assert [e[2] for e in got] == [EXIT_MARK + "  "]


# --- cp-070: read_transcript_entries session attribution ---------------------------

def test_read_transcript_entries_session_id_reads_own_file(cc_home,
                                                           monkeypatch,
                                                           tmp_path):
    # session_id -> exactly <sid>.jsonl, even when another file is
    # NEWER by mtime (the --continue replay file)
    root = _transcript_root(tmp_path, monkeypatch)
    _write_jsonl(root, "-Users-devx-proj", "replay.jsonl", [
        {"type": "user", "timestamp": "2026-09-30T14:10:27.000000Z",
         "message": {"content": "replay"}}], mtime=2_000.0)
    _write_jsonl(root, "-Users-devx-proj", "sid-abc.jsonl", [
        {"type": "user", "timestamp": "2026-09-30T14:00:00.000000Z",
         "message": {"content": "own"}}], mtime=1_000.0)
    got = watchdogd.read_transcript_entries("/Users/devx/proj", "sid-abc")
    assert [e[2] for e in got] == ["own"]


def test_read_transcript_entries_session_id_missing_falls_back_newest(
        cc_home, monkeypatch, tmp_path):
    # Phase 21.3 (2026-10-01 connector-066 bug): the recorded session_id is
    # STALE (its <sid>.jsonl does not exist) because CC took a new session id
    # on a --resume/rotation without cc-continuity updating state.json. The
    # process's REAL transcript is the dir's newest file -> fall back to it
    # (the legacy dir-newest read) instead of giving up (None). An empty /
    # missing dir still yields None (no file to read).
    root = _transcript_root(tmp_path, monkeypatch)
    _write_jsonl(root, "-Users-devx-proj", "other.jsonl", [
        {"type": "user", "timestamp": "2026-09-30T14:10:27.000000Z",
         "message": {"content": "other"}}], mtime=2_000.0)
    got = watchdogd.read_transcript_entries("/Users/devx/proj",
                                            "sid-missing")
    assert [e[2] for e in got] == ["other"]
    # ...a dir with NO files at all still returns None (nothing to read)
    assert watchdogd.read_transcript_entries("/Users/devx/elsewhere",
                                            "sid-missing") is None


@pytest.mark.parametrize("sid", [None, ""])
def test_read_transcript_entries_session_id_empty_keeps_legacy(
        cc_home, monkeypatch, tmp_path, sid):
    # None / empty session_id -> the legacy dir-newest read
    root = _transcript_root(tmp_path, monkeypatch)
    _write_jsonl(root, "-Users-devx-proj", "old.jsonl", [
        {"type": "user", "timestamp": "2026-09-28T20:00:00.000000Z",
         "message": {"content": "old"}}], mtime=1_000.0)
    _write_jsonl(root, "-Users-devx-proj", "new.jsonl", [
        {"type": "user", "timestamp": "2026-09-28T21:00:00.000000Z",
         "message": {"content": "new"}}], mtime=2_000.0)
    got = watchdogd.read_transcript_entries("/Users/devx/proj", sid)
    assert [e[2] for e in got] == ["new"]


# --- check_exit_hangs ---------------------------------------------------------------

def test_check_exit_hangs_kills_hung_claude(cc_home, monkeypatch):
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io()
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert actions[0]["pid"] == 24822
    assert actions[0]["mode"] == "term"
    assert actions[0]["key"] == "proj-a"
    assert actions[0]["nnn"] == 1
    assert actions[0]["age_s"] == pytest.approx(40.0)
    assert len(pushes) == 1
    ev, slug, nnn, fields = pushes[0]
    assert ev == "exit_hang"
    assert slug == "proj-a"
    assert nnn == 1
    assert fields["mode"] == "term"
    assert fields["age_s"] == pytest.approx(40.0)
    assert 24822 in ctx.exit_hang_killed


def test_check_exit_hangs_logs_kill_line(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(kill_result="kill")
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    log = (paths.logs_dir() / "watchdog.log").read_text(encoding="utf-8")
    line = json.loads(log.strip().splitlines()[-1])
    assert line["events"] == ["exit_hang_killed:proj-a"]
    assert line["key"] == "proj-a"
    assert line["nnn"] == 1
    assert line["pane"] == "cc-proj-a-0a9b"
    assert line["pid"] == 24822
    assert line["mode"] == "kill"
    assert line["age_s"] == pytest.approx(40.0)
    assert "ts" in line


def test_check_exit_hangs_dead_race_no_push_no_log(cc_home, monkeypatch):
    # the kill lost the race (someone else reaped it): no push, NO log
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(kill_result="dead")
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert 24822 in ctx.exit_hang_killed
    assert pushes == []
    assert not (paths.logs_dir() / "watchdog.log").is_file()


def test_check_exit_hangs_noop_pass_writes_no_log(cc_home, monkeypatch):
    # marker still within the grace: nothing happens and NOTHING is logged
    # (a log line every 5s would bloat the file by MBs a day)
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript=_exit_entries(age=5.0))
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert not (paths.logs_dir() / "watchdog.log").is_file()


def test_check_exit_hangs_skips_turn_active(cc_home, monkeypatch):
    # mid-turn /exit: the Stop hook flips turn_active False later; until
    # then the process is never touched (and the ps scan is skipped too)
    _capture_pushes(monkeypatch)
    _mk(turn_active=True)
    io = _hung_io()
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert io.read_calls == []
    assert io.ps_calls == 0


def test_check_exit_hangs_activity_after_marker_not_killed(cc_home,
                                                            monkeypatch):
    # a prompt landing after the marker = the user gave up on /exit
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript=_exit_entries(
        after=[(9_995.0, "user", "yeni prompt")]))
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []


def test_check_exit_hangs_no_tmux_is_noop(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(sessions=None)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert io.ps_calls == 0


def test_check_exit_hangs_unresolvable_target_skipped(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)  # stale tmux_session, no prefix match
    io = _hung_io(sessions=["cc-other-1"])
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert io.ps_calls == 0


def test_check_exit_hangs_panes_none_skipped(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(panes={"cc-proj-a-0a9b": None})
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []


def test_check_exit_hangs_no_claude_in_tree_skipped(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    procmap = {24817: (1, "sh -c x"), 24822: (24817, "zsh")}
    io = _hung_io(procmap=procmap)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert io.read_calls == []  # no claude -> no transcript read


def test_check_exit_hangs_pid_not_alive_skipped(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(alive_pids=set())
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []


def test_check_exit_hangs_killed_pid_not_rekilled(cc_home, monkeypatch):
    # a zombie keeps kill(pid, 0) succeeding: the killed-set (daemon
    # lifetime) is the re-kill / re-push guard
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io()
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert len(pushes) == 1


def test_check_exit_hangs_ps_tree_none_is_noop(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(procmap=None)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []


def test_check_exit_hangs_ps_tree_raises_never_propagates(cc_home,
                                                           monkeypatch):
    # P10-D12: a fast-pass exception must not escape to the loop
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io()

    def boom():
        raise RuntimeError("ps exploded")

    io.ps_snapshot = boom
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)  # must not raise
    assert io.kills == []


def test_check_exit_hangs_env_limit_respected(cc_home, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_EXIT_HANG_LIMIT", "60")
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript=_exit_entries(age=40.0))  # 40 s < 60 s grace
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []


def test_check_exit_hangs_same_target_twice_kills_highest_once(
        cc_home, monkeypatch):
    # two live records of one project share the target (in-place
    # rotation): kill ONCE, and the push carries the HIGHEST nnn
    pushes = _capture_pushes(monkeypatch)
    _mk(nnn=1, turn_active=False)
    _mk(nnn=2, turn_active=False)  # same default tmux_session
    io = _hung_io()
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert io.ps_calls == 1
    assert len(pushes) == 1
    assert pushes[0][2] == 2
    assert actions[0]["nnn"] == 2


def test_check_exit_hangs_each_project_killed_ps_once(cc_home, monkeypatch):
    _capture_pushes(monkeypatch)
    _mk(key="proj-a", nnn=1, turn_active=False, tmux="cc-proj-a-0a9b")
    _mk(key="proj-b", nnn=1, turn_active=False, tmux="cc-proj-b-0b9c")
    panes = {"cc-proj-a-0a9b": [_PANE],
             "cc-proj-b-0b9c": [
                 {"pane": "%26", "pid": 25646, "path": "/Users/devx/other"}]}
    procmap = {24817: (1, "sh -c x"), 24822: (24817, "claude -n a"),
               25646: (1, "sh -c y"), 25652: (25646, "claude -n b")}
    io = _hung_io(sessions=["cc-proj-a-0a9b", "cc-proj-b-0b9c"],
                  panes=panes, procmap=procmap,
                  alive_pids={24822, 25652})
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert sorted(io.kills) == [24822, 25652]
    assert io.ps_calls == 1  # one ps scan for the whole pass
    assert {a["key"] for a in actions} == {"proj-a", "proj-b"}


# --- cp-069: the marker must be this process's own /exit -------------------------
#
# The 2026-09-30 incident: every FRESH connector claude was killed ~5 s
# after launch because read_transcript_entries picked the dir's newest
# transcript — a 2-day-old file whose tail is an OLD /exit marker. The
# guard: a marker older than the process START is not this process's /exit.

def test_check_exit_hangs_stale_marker_fresh_process_not_killed(
        cc_home, monkeypatch):
    # the dir's newest transcript holds a 2-hour-old marker; the pane's
    # claude started 10 s ago (a --continue replay / a pre-start
    # transcript) -> no kill, no push, no log, NOT zombie-guarded
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript=_exit_entries(now=10_000.0, age=7200.0),
                  process_start=10_000.0 - 10.0)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert pushes == []
    assert not (paths.logs_dir() / "watchdog.log").is_file()
    assert 24822 not in ctx.exit_hang_killed


def test_check_exit_hangs_unknown_process_start_not_killed(cc_home,
                                                           monkeypatch):
    # ps race: the start time is unknown -> degrade to no-kill (a
    # destructive action never runs on an unverified state; the next
    # pass retries)
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(process_start=None)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert pushes == []
    assert not (paths.logs_dir() / "watchdog.log").is_file()
    assert 24822 not in ctx.exit_hang_killed


def test_process_start_ts_ps_command_and_seconds():
    cmds = []

    def run(cmd, **kw):
        cmds.append(cmd)
        return _P(0, b"  03:45\n")

    got = watchdogd.process_start_ts(777, now=lambda: 100_000.0, runner=run)
    assert cmds == [["ps", "-o", "etime=", "-p", "777"]]
    assert got == 100_000.0 - 225.0


def test_process_start_ts_hours_and_days_forms():
    # BSD etime: H:MM:SS (>=1h) and D-H:MM:SS (>=1d)
    h = watchdogd.process_start_ts(1, now=lambda: 100_000.0,
                                   runner=lambda *a, **k: _P(0, b"03:08:00\n"))
    assert h == 100_000.0 - (3 * 3600 + 8 * 60)
    d = watchdogd.process_start_ts(1, now=lambda: 100_000.0,
                                   runner=lambda *a, **k:
                                   _P(0, b"03-03:49:06\n"))
    assert d == 100_000.0 - (3 * 86400 + 3 * 3600 + 49 * 60 + 6)


def test_process_start_ts_failure_none():
    # rc != 0 / runner error / malformed output -> None (the caller
    # degrades to no-kill)
    assert watchdogd.process_start_ts(1, now=lambda: 0.0,
                                      runner=lambda *a, **k: _P(1, b"")) \
        is None

    def boom(*a, **k):
        raise OSError("no ps")

    assert watchdogd.process_start_ts(1, now=lambda: 0.0, runner=boom) \
        is None
    assert watchdogd.process_start_ts(1, now=lambda: 0.0,
                                      runner=lambda *a, **k: _P(0, b"junk\n")) \
        is None


# --- cp-070: the marker must be in THIS session's own transcript -------------------
#
# The 2026-09-30 16:10 incident: a --continue replay of an old
# conversation wrote a fresh /exit marker to its OWN (old) transcript
# file; the dir-newest read attributed it to a DIFFERENT live session's
# CC (started before the marker, so the cp-069 start-time guard passed)
# -> false-positive kill. Fix: read the session's OWN transcript
# (session_id -> <sid>.jsonl); no own file -> no marker -> no kill.

def test_check_exit_hangs_other_sessions_marker_not_killed(cc_home,
                                                           monkeypatch):
    # the session's own transcript is empty; the dir's NEWEST file
    # (another session) holds a fresh marker -> no kill, no push, no log
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript={
        "sid-proj-a-1": [],
        "<newest>": _exit_entries(),
    })
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert pushes == []
    assert not (paths.logs_dir() / "watchdog.log").is_file()
    assert 24822 not in ctx.exit_hang_killed


def test_check_exit_hangs_stale_sid_falls_back_kills(cc_home, monkeypatch):
    # Phase 21.3 (the 2026-10-01 connector-066 bug): the record's
    # session_id is STALE (no own <sid>.jsonl), but the process's REAL
    # transcript — the dir's newest file — holds a fresh /exit marker and
    # the process is alive past the grace. The killer falls back to the
    # process's own transcript and kills. (Was `..._own_file_missing_not_
    # killed`: cp-070's "own file missing -> no kill" was the bug — a
    # stale id made the killer give up before finding the real marker.)
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript={"<newest>": _exit_entries()})
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert [a["nnn"] for a in actions] == [1]
    assert pushes  # the push fires


def test_check_exit_hangs_stale_sid_no_marker_no_kill(cc_home, monkeypatch):
    # Phase 21.3 safety: stale session_id, but the process's real
    # transcript (dir newest) has NO /exit marker (the CC is just idle,
    # not hung on /exit) -> no kill. An idle session must never be killed
    # by a stray fallback read.
    _mk(turn_active=False)
    io = _hung_io(transcript={"<newest>": [
        (9_960.0, "assistant", "still working, here is a result"),
    ]})
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert 24822 not in ctx.exit_hang_killed


def test_check_exit_hangs_stale_sid_fresh_process_not_killed(cc_home,
                                                             monkeypatch):
    # Phase 21.3 / 16:10 guard: stale session_id, the dir's newest holds a
    # fresh marker, but THIS process is FRESH (started after the marker).
    # The cp-069 start-time guard rejects a marker older than the process
    # start -> no kill. (The realistic 16:10 replay: the foreign marker
    # predates this process, so it is not this process's /exit.)
    _mk(turn_active=False)
    # marker at 9960 (now=10000, age 40); process started at 9990 (fresh)
    io = _hung_io(transcript={"<newest>": _exit_entries()},
                  process_start=9_990.0)
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == []
    assert 24822 not in ctx.exit_hang_killed


def test_check_exit_hangs_own_marker_killed(cc_home, monkeypatch):
    # the marker IS in the session's own transcript -> still killed
    # (guard against over-correction of the cp-069 guard)
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(transcript={"sid-proj-a-1": _exit_entries()})
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert [a["nnn"] for a in actions] == [1]
    assert pushes  # the push still fires


def test_check_exit_hangs_no_session_id_falls_back_to_newest(cc_home,
                                                             monkeypatch):
    # a record WITHOUT session_id keeps the legacy dir-newest read
    # (old-format degrade path; the cp-069 start-time guard still applies)
    _mk(turn_active=False)
    state.update_fields("proj-a", 1, session_id=None)
    io = _hung_io()
    ctx = _exit_ctx(io)
    actions = watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert [a["nnn"] for a in actions] == [1]


# --- cp-071: zombie-session watcher (Phase 23 fix a) --------------------------------
#
# A clean /exit whose CC died can leave the tmux session ALIVE on the
# orphaned progress-pane (no CC, no restart — launch is one-shot). The
# 2026-09-30 c19b incident: the user's "kill it" commands landed in the
# progress pane, the session sat as a zombie. The watcher kills it:
# off=True record + tmux_session still in list_sessions + no LIVE record
# resolves to it + NO claude in any of its panes + off for > grace.

# a progress-pane: a python process (NOT claude) with a child — no claude
# anywhere in the tree, so find_claude_pid returns None for the pane
_ZOMBIE_PANE = {"pane": "%99", "pid": 50001, "path": "/Users/devx/proj"}
_ZOMBIE_PROCMAP = {50001: (1,
                           "python3 /opt/homebrew/bin/cc-continuity "
                           "progress-pane"),
                   50002: (50001, "sleep 30")}
# a pane whose tree DOES contain a live claude (the must-not-kill case)
_CLAUDE_PANE = {"pane": "%23", "pid": 24817, "path": "/Users/devx/proj"}
_CLAUDE_PROCMAP = {24817: (1, "/bin/sh /usr/bin/command claude -n old"),
                   24822: (24817, "claude --dangerously-skip-permissions")}
_ZS = "cc-proj-a-0a9b"  # the zombie tmux session name


def _set_updated(key, nnn, epoch):
    """Pin the record's ``updated`` to ``epoch`` (a tz-aware ISO string
    parse_entry_ts round-trips) — controls the off-grace in the tests."""
    p = paths.session_dir(key, nnn) / "state.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["updated"] = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    p.write_text(json.dumps(rec), encoding="utf-8")


def _m_off(key="proj-a", nnn=1, tmux=_ZS, off=True, updated_epoch=9000.0,
           turn_active=False):
    """An off record (the zombie candidate). updated_epoch pins the
    off-time: 9000.0 is 1000 s before the pinned now (10_000.0) → past
    the 5 s grace (Phase 25); 9996.0 is 4 s → within grace."""
    state.create_session_state(key, f"{key}-{nnn:03d}", nnn,
                               session_id=f"sid-{key}-{nnn}",
                               initial_state="ACTIVE")
    if tmux is not None:
        state.update_fields(key, nnn, tmux_session=tmux)
    if turn_active:
        state.update_fields(key, nnn, turn_active=True)
    if off:
        state.update_fields(key, nnn, off=True)
    _set_updated(key, nnn, updated_epoch)
    return paths.session_dir(key, nnn)


def _zombie_io(panes=None, procmap=_ZOMBIE_PROCMAP, sessions=(_ZS,),
               kill_session_result=True):
    return FakeIO(sessions=list(sessions), panes={_ZS: (panes
                                                        if panes is not None
                                                        else [_ZOMBIE_PANE])},
                  procmap=procmap, kill_session_result=kill_session_result)


def _zline():
    return {"ts": 10_000.0, "events": []}


def test_check_zombie_sessions_kills_no_claude_session(cc_home, monkeypatch):
    # the happy path: off record, session alive, no live record, no claude,
    # past grace -> kill + push + dedup + log event
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io()
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == [_ZS]
    assert len(actions) == 1 and actions[0]["session"] == _ZS
    assert _ZS in ctx.zombie_killed
    assert any(ev[0] == "zombie_cleaned" for ev in pushes)
    assert any(ev.startswith("zombie_cleaned") for ev in line["events"])


def test_check_zombie_sessions_live_record_points_to_s_no_kill(cc_home,
                                                               monkeypatch):
    # a LIVE record resolves to the same session -> never kill it
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    _mk(key="proj-a", nnn=2, tmux=_ZS, turn_active=False)  # live record
    io = _zombie_io()
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed


def test_check_zombie_sessions_stale_live_fallback_no_shield(cc_home,
                                                             monkeypatch):
    # 2026-10-01 connector-3fe6 regression: a LIVE record whose OWN
    # tmux_session is dead (absent from list_sessions) but which
    # resolve_pane would prefix-fall-back to the orphaned zombie session
    # must NOT shield it. Only a live record's DIRECT tmux_session
    # protects a pane; a stale record's fallback target is not ownership.
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)  # off zombie candidate -> _ZS
    _mk(key="proj-a", nnn=2, tmux="cc-proj-a-dead",
        turn_active=False)  # live record, dead pane
    io = _zombie_io()  # sessions=(_ZS,) only
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == [_ZS]
    assert len(actions) == 1 and actions[0]["session"] == _ZS
    assert _ZS in ctx.zombie_killed
    assert any(ev[0] == "zombie_cleaned" for ev in pushes)


def test_check_zombie_sessions_claude_in_pane_no_kill(cc_home, monkeypatch):
    # a live claude in the session's panes -> NOT a zombie -> no kill
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io(panes=[_CLAUDE_PANE], procmap=_CLAUDE_PROCMAP)
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed


def test_check_zombie_sessions_within_grace_no_kill(cc_home, monkeypatch):
    # Phase 25: off only 4 s ago (< the 5 s grace) -> wait, do not kill
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9996.0)  # now=10_000 -> 4 s ago (< 5 s grace)
    io = _zombie_io()
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed


def test_check_zombie_sessions_not_in_tmux_no_kill(cc_home, monkeypatch):
    # the session is already gone (not in list_sessions) -> nothing to kill
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io(sessions=[])  # the session no longer exists
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed


def test_check_zombie_sessions_no_tmux_session_field_no_kill(cc_home,
                                                             monkeypatch):
    # an off record with NO tmux_session field -> nothing to resolve
    pushes = _capture_pushes(monkeypatch)
    _m_off(tmux=None, updated_epoch=9000.0)
    io = _zombie_io()
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []


def test_check_zombie_sessions_empty_panes_no_kill(cc_home, monkeypatch):
    # list_panes returns no panes -> cannot verify it is a zombie -> no kill
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io(panes=[])
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed


def test_check_zombie_sessions_kill_rc0_not_deduped(cc_home, monkeypatch):
    # kill rc != 0 (already gone / race) -> NOT added to the dedup set, so
    # the next tick retries; still no push (nothing was killed)
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io(kill_session_result=False)
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == [_ZS]  # it tried
    assert actions == []
    assert pushes == []
    assert _ZS not in ctx.zombie_killed  # not deduped -> retries


def test_check_zombie_sessions_dedup_no_rekill(cc_home, monkeypatch):
    # already cleaned this daemon lifetime -> never touched again
    pushes = _capture_pushes(monkeypatch)
    _m_off(updated_epoch=9000.0)
    io = _zombie_io()
    ctx = _exit_ctx(io)
    ctx.zombie_killed.add(_ZS)  # pre-populated (a prior tick cleaned it)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert pushes == []


def test_check_zombie_sessions_tmux_unavailable_no_kill(cc_home, monkeypatch):
    # no tmux server (list_sessions -> None) -> cannot verify -> no kill
    _m_off(updated_epoch=9000.0)
    io = FakeIO(sessions=None, panes={_ZS: [_ZOMBIE_PANE]},
                procmap=_ZOMBIE_PROCMAP, kill_session_result=True)
    ctx = _exit_ctx(io)
    line = _zline()
    actions = watchdogd.check_zombie_sessions(ctx, line)
    assert io.kill_session_calls == []
    assert actions == []
    assert _ZS not in ctx.zombie_killed


# --- kill_tmux_session (GATE: zombie session / progress-pane cleanup) ---------------
#
# Live validation 2026-09-29: killing the hung claude closed its pane, but
# the session survived on the orphaned progress-pane (no CC, no restart —
# launch is one-shot). The approved GATE: after the kill, kill the whole
# session.

def test_kill_tmux_session_ok():
    cmds = []

    def run(cmd, **kw):
        cmds.append(cmd)
        return _P(0, b"")

    assert watchdogd.kill_tmux_session("cc-proj-a-0a9b", runner=run) is True
    assert cmds[0][1:4] == ["kill-session", "-t", "cc-proj-a-0a9b"]


def test_kill_tmux_session_already_gone_is_false():
    # the pane died with the session in the meantime: rc != 0 = degrade,
    # not an error (the cleanup is best-effort)
    assert watchdogd.kill_tmux_session("s",
                                       runner=lambda *a, **k: _P(1, b"")) \
        is False


def test_kill_tmux_session_runner_error_is_false():
    def boom(*a, **k):
        raise OSError("tmux gone")

    assert watchdogd.kill_tmux_session("s", runner=boom) is False


def test_check_exit_hangs_kills_session_after_kill(cc_home, monkeypatch):
    # GATE: the hung claude was the session's main pane; without cleanup
    # the orphaned progress-pane keeps the session alive as a zombie
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io()
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert io.kill_session_calls == ["cc-proj-a-0a9b"]


def test_check_exit_hangs_dead_race_no_session_kill(cc_home, monkeypatch):
    # the kill lost the race: WE killed nothing, so the session is left
    # as-is (no cleanup)
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(kill_result="dead")
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert io.kill_session_calls == []


def test_check_exit_hangs_session_kill_failure_degrades(cc_home, monkeypatch):
    # a failed cleanup (tmux error) must not eat the push or the log,
    # and must not escape to the loop (P10-D12)
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io(kill_session_result=RuntimeError("tmux exploded"))
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)  # must not raise
    assert io.kills == [24822]
    assert io.kill_session_calls == ["cc-proj-a-0a9b"]
    assert len(pushes) == 1
    log = (paths.logs_dir() / "watchdog.log").read_text(encoding="utf-8")
    line = json.loads(log.strip().splitlines()[-1])
    assert line["events"] == ["exit_hang_killed:proj-a"]


def test_check_exit_hangs_rekilled_pass_no_second_session_kill(
        cc_home, monkeypatch):
    # second pass: the pid is in the killed-set -> no kill, no cleanup
    _capture_pushes(monkeypatch)
    _mk(turn_active=False)
    io = _hung_io()
    ctx = _exit_ctx(io)
    watchdogd.check_exit_hangs(ctx)
    watchdogd.check_exit_hangs(ctx)
    assert io.kills == [24822]
    assert io.kill_session_calls == ["cc-proj-a-0a9b"]


# --- cp-071: FROZEN progress-pane skip (Phase 23 fix b) -----------------------
_PROGRESS_TAIL = "  ◆ 42% Active Usage"


def _frozen_io(panes=(_ZOMBIE_PANE,), procmap=_ZOMBIE_PROCMAP,
               pane=_PROGRESS_TAIL):
    return FakeIO(pane=pane, sessions=["cc-proj-a-0a9b"],
                  panes={"cc-proj-a-0a9b": list(panes)}, procmap=procmap)


def test_frozen_progress_pane_tail_match(cc_home, monkeypatch):
    # captured tail is a progress usage line -> skip (no procmap needed)
    ctx = _exit_ctx(_frozen_io())
    assert watchdogd._frozen_progress_pane(ctx, _ZS, _PROGRESS_TAIL) is True


def test_frozen_progress_pane_no_claude_in_panes(cc_home, monkeypatch):
    # non-progress tail, but no claude in any pane (progress-only) -> skip
    ctx = _exit_ctx(_frozen_io(pane="some static line"))
    assert watchdogd._frozen_progress_pane(ctx, _ZS, "some static line") \
        is True


def test_frozen_progress_pane_has_claude_not_progress(cc_home, monkeypatch):
    # a live claude in a pane -> NOT progress-only -> no skip
    io = _frozen_io(panes=(_CLAUDE_PANE,), procmap=_CLAUDE_PROCMAP,
                    pane="some static line")
    ctx = _exit_ctx(io)
    assert watchdogd._frozen_progress_pane(ctx, _ZS, "some static line") \
        is False


def test_frozen_progress_pane_no_procmap_degrades(cc_home, monkeypatch):
    # no procmap/panes + non-progress tail -> cannot verify -> no skip
    io = FakeIO(pane="some static line", sessions=["cc-proj-a-0a9b"],
                panes={"cc-proj-a-0a9b": []}, procmap={})
    ctx = _exit_ctx(io)
    assert watchdogd._frozen_progress_pane(ctx, _ZS, "some static line") \
        is False


def test_tick_frozen_progress_pane_no_action(cc_home, monkeypatch):
    # a LIVE session whose active pane is a progress-only display (no CC)
    # is FROZEN-classified; the fix must NOT push freeze_detected / send
    # frozen_esc (the 14:03/16:04 misattribution)
    pushes = _capture_pushes(monkeypatch)
    _mk(turn_active=False)  # live record -> pane cc-proj-a-0a9b
    io = _frozen_io(pane="  ◆ 42% Active Usage")  # static progress display
    ctx = _exit_ctx(io)
    watchdogd.tick(ctx)          # baseline
    watchdogd.tick(ctx)          # unchanged
    l3 = watchdogd.tick(ctx)     # FROZEN
    assert l3["sessions"][0]["outcome"] == "FROZEN"
    assert not any(ev[0] == "freeze_detected" for ev in pushes)
    assert io.sends == []
