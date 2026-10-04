"""Phase 10 integration E2E (spec §5): the full daemon path with real
curl probes, a fake tmux binary, and a mock ntfy server.

Real I/O, test-owned clock: tick() is driven directly so the scenario
runs in seconds; every byte that crosses a process boundary (HTTP,
subprocess argv, store writes, the ntfy wire) is real. The DGX box is
NEVER touched (P10-D1) — the health endpoint is 127.0.0.1 (P10-D13).
"""

import http.server
import json
import os
import shutil
import threading
import time

import pytest

from cc_continuity import config, ledger, paths, state
from cc_continuity import watchdogd
from cc_continuity.watchdog import HealthState
from cc_continuity.watchdogd import TickCtx

SESSION = "cc-e2e-proj-0a9b"


class _Server:
    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      handler)
        self.port = self.httpd.server_address[1]
        self.handler = handler
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _health_server():
    box = {"code": 200}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(box["code"])
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    return _Server(H), box


def _ntfy_server():
    pushes = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode()
            # http.server decodes header values as latin-1 (RFC 7230);
            # curl sends the emoji title as UTF-8 bytes — re-encode
            # to recover it (same as test_notify_integration).
            pushes.append((self.headers.get("Title", "")
                           .encode("latin-1").decode("utf-8"),
                           self.headers.get("Priority", ""), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = _Server(H)
    server.pushes = pushes  # the inbox both _wait/_ntfy_inbox read
    return server, pushes


def _wait(server, n, timeout=5.0):
    """fire-and-forget Popen pushes land asynchronously — poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(server.pushes) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"only {len(server.pushes)}/{n} pushes")


def _ntfy_inbox(server):
    # the pushes list is the closure captured by _ntfy_server; the
    # server instance carries it
    return server.pushes


def _fake_tmux(tmp_path, pane_text="work output line\n❯ "):
    d = tmp_path / "e2etmux"
    d.mkdir()
    log, pane, sessions = d / "calls.log", d / "pane.txt", d / "sessions.txt"
    pane.write_text(pane_text, encoding="utf-8")
    sessions.write_text(SESSION + "\n", encoding="utf-8")
    script = d / "tmux"
    script.write_text(
        "#!/bin/sh\necho \"$@\" >> " + str(log) + "\ncase \"$1\" in\n"
        "  capture-pane) cat " + str(pane) + " ;;\n"
        "  list-sessions) cat " + str(sessions) + " ;;\n"
        "esac\nexit 0\n", encoding="utf-8")
    os.chmod(script, 0o755)
    return script, pane, log


def test_e2e_dead_turn_down_wait_up_nudge_enter_verified(cc_home, tmp_path,
                                                         monkeypatch):
    if shutil.which("curl") is None:
        pytest.skip("curl required (P10-D4 probe)")
    monkeypatch.delenv("CC_CONTINUITY", raising=False)
    health, hstate = _health_server()
    ntfy, _ = _ntfy_server()
    ftx, pane, calls = _fake_tmux(tmp_path)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(ftx))

    # store: one dead turn — stale ledger, turn never stopped, at prompt
    key = "e2e-proj"
    state.create_session_state(key, "e2e-proj-001", 1, session_id="sid-e2e",
                               initial_state="ACTIVE")  # nudge-eligible
    state.update_fields(key, 1, turn_active=True, tmux_session=SESSION)
    sdir = paths.session_dir(key, 1)
    ledger.append(sdir, {"type": "user_prompt", "ts": 10_000.0 - 3_000.0})

    (cc_home / "config.json").write_text(json.dumps({
        "notify_enabled": True, "notify_endpoint": ntfy.url(),
        "watchdog_enabled": True, "watchdog_interval_s": 300,
        "watchdog_stale_minutes": 45, "watchdog_max_nudges": 3,
        "watchdog_nudge_text": "continue",
        "watchdog_health_url": health.url("/health")}), encoding="utf-8")
    cfg = config.load_config()
    ctx = TickCtx(cfg=cfg, health=HealthState(), episodes={},
                  prev_captures={}, froze=set(),
                  probe=lambda: watchdogd.probe_health(cfg),
                  capture=watchdogd.capture_pane,
                  list_sessions=watchdogd.list_tmux_sessions,
                  send=watchdogd.send_keys,
                  send_text=watchdogd.send_literal, now=10_000.0)

    # 1-2: the server dies → DOWN after 2 failed probes; the dead turn
    #      must WAIT for the server (a nudge is a model call, P10-D7.1)
    hstate["code"] = 500
    l1 = watchdogd.tick(ctx)
    assert l1["sessions"][0]["outcome"] == "TURN_DEAD"
    assert "waiting_for_dgx" in " ".join(l1["events"])
    ctx.now += 300
    l2 = watchdogd.tick(ctx)
    assert "dgx_down" in l2["events"]
    # no pane WRITE while DOWN — a nudge is a model call (P10-D7.1);
    # read-only list-sessions/capture-pane polling is expected
    sent = calls.read_text(encoding="utf-8").splitlines()
    assert not any(l.startswith("send-keys") for l in sent)

    # 3: the server is back → dgx_up, then the pending nudge fires
    hstate["code"] = 200
    ctx.now += 300
    l3 = watchdogd.tick(ctx)
    assert l3["events"][0] == "dgx_up"
    assert any(e.startswith("turn_dead:") for e in l3["events"])
    sent = calls.read_text(encoding="utf-8").splitlines()
    # Phase 19: a STANDALONE send-keys Escape force-cancels the stuck
    # turn, then ONE send-keys call carries the enriched MULTI-LINE
    # text + Enter. The fake tmux's `echo "$@"` logs that call as one
    # line per text line — the first ends at the base line, the last
    # ends at " Enter". (No new Turkish literals: "continue" is this
    # test's configured base; the marker is ASCII.)
    esc = next(i for i, l in enumerate(sent)
               if l.startswith("send-keys") and l.endswith("Escape"))
    txt = next(i for i, l in enumerate(sent)
               if l.startswith("send-keys") and l.endswith(" continue"))
    assert esc < txt
    assert any(l.endswith(" Enter") for l in sent)
    assert any("[cc-continuity watchdog]" in l for l in sent)
    assert ledger.read(sdir)[-1]["type"] == "watchdog_nudge"
    _wait(ntfy, 3)
    assert [p[0] for p in _ntfy_inbox(ntfy)] == [
        "🔌 cc-continuity: model server DOWN",
        "🔌 cc-continuity: model server back",
        "⟲ cc-continuity: turn died — recovering"]

    # 4: the Enter was lost — the enriched (multi-line) nudge sits
    #    unsubmitted in the input box (11:30): the ❯ glyph is on the
    #    FIRST input line only, so the pane is NOT "at prompt" → WAIT
    #    (not TURN_DEAD) and the pending check matches the SENT text's
    #    tail line → a BARE Enter, no new attempt. The pane is derived
    #    from the ledger's nudge text — no Turkish re-typed here.
    nudge = [e for e in ledger.read(sdir)
             if e["type"] == "watchdog_nudge"][-1]
    nudge_ts = nudge["ts"]
    pane.write_text("work output line\n❯ " + nudge["text"] + "\n",
                    encoding="utf-8")
    ctx.now += 300
    l4 = watchdogd.tick(ctx)
    assert l4["sessions"][0]["outcome"] == "ALIVE"  # capture CHANGED
    ctx.now += 300
    l5 = watchdogd.tick(ctx)
    assert l5["sessions"][0]["outcome"] == "WAIT"
    sent = calls.read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("send-keys") and "continue" not in l
               and "Enter" in l for l in sent)
    n = [e for e in ledger.read(sdir)
         if e["type"] == "watchdog_nudge"]
    assert len(n) == 1  # SEND_ENTER does not count (P10-D7)

    # 5: the nudge LANDING — CC records the user prompt (ts > nudge_ts)
    #    → VERIFIED → turn_recovered (the 11:30 recovery, P10-D7.2)
    ledger.append(sdir, {"type": "user_prompt", "ts": nudge_ts + 10.0})
    ctx.now += 300
    l6 = watchdogd.tick(ctx)
    assert l6["sessions"][0]["outcome"] == "VERIFIED"
    _wait(ntfy, 4)
    inbox = _ntfy_inbox(ntfy)
    assert [p[0] for p in inbox] == [
        "🔌 cc-continuity: model server DOWN",
        "🔌 cc-continuity: model server back",
        "⟲ cc-continuity: turn died — recovering",
        "✅ cc-continuity: turn recovered"]
    assert [p[1] for p in inbox] == ["urgent", "high", "high", "default"]
    assert "user_prompt" in inbox[3][2]  # the body names the evidence
    health.close()
    ntfy.close()


def test_e2e_restart_detection_no_dead_turn(cc_home, tmp_path, monkeypatch):
    """The user-reported 2026-09-22 DGX restart scenario: a plain
    server restart (no dead turn) produces EXACTLY [dgx_down, dgx_up]
    within 2×interval and touches no pane (P10-D4/P10-D12)."""
    if shutil.which("curl") is None:
        pytest.skip("curl required (P10-D4 probe)")
    monkeypatch.delenv("CC_CONTINUITY", raising=False)
    health, hstate = _health_server()
    ntfy, _ = _ntfy_server()
    ftx, _pane, calls = _fake_tmux(tmp_path)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(ftx))
    (cc_home / "config.json").write_text(json.dumps({
        "notify_enabled": True, "notify_endpoint": ntfy.url(),
        "watchdog_enabled": True, "watchdog_interval_s": 300,
        "watchdog_stale_minutes": 45, "watchdog_max_nudges": 3,
        "watchdog_nudge_text": "continue",
        "watchdog_health_url": health.url("/health")}), encoding="utf-8")
    cfg = config.load_config()
    ctx = TickCtx(cfg=cfg, health=HealthState(), episodes={},
                  prev_captures={}, froze=set(),
                  probe=lambda: watchdogd.probe_health(cfg),
                  capture=watchdogd.capture_pane,
                  list_sessions=watchdogd.list_tmux_sessions,
                  send=watchdogd.send_keys,
                  send_text=watchdogd.send_literal, now=10_000.0)

    hstate["code"] = 500
    watchdogd.tick(ctx)            # failure 1: no transition yet
    ctx.now += 300
    l2 = watchdogd.tick(ctx)       # failure 2 → DOWN
    assert "dgx_down" in l2["events"]
    hstate["code"] = 200
    ctx.now += 300
    l3 = watchdogd.tick(ctx)       # the restart recovery
    assert l3["events"][:1] == ["dgx_up"]
    _wait(ntfy, 2)
    assert [p[0] for p in _ntfy_inbox(ntfy)] == [
        "🔌 cc-continuity: model server DOWN",
        "🔌 cc-continuity: model server back"]
    # no live session → no pane touch at all (P10-D4/P10-D12)
    calls_text = calls.read_text(encoding="utf-8")
    assert "send-keys" not in calls_text
    assert "capture-pane" not in calls_text
    health.close()
    ntfy.close()
