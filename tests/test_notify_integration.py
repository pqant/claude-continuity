"""Phase 9 integration — one full lifecycle against a real (mock) ntfy
server: the EXACT spec §5 push sequence, production code paths only
(register -> warn -> pending -> checkpoint -> rotate -> resume -> end).
The only mock is the HTTP endpoint itself; curl, config, hooks, rotator
and checkpoint are all real. P9-D7: delivery is best-effort, so the
server capture is polled, never assumed instant."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cc_continuity import (checkpoint, config, paths, project, rotator,
                           state, thresholds)
from hookutil import (SID, make_repo, payload, register_session, run_hook)
from test_integration_phase6 import NEW_SID, _hook_cmd, _world_payloads
from test_rotator import _DRAFT, fake_tmux  # noqa: F401


class _NtfyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        # http.server decodes header values as latin-1 (RFC 7230); curl
        # sends the emoji title as UTF-8 bytes — re-encode to recover it.
        self.server.pushes.append({
            "title": self.headers.get("Title", "")
                    .encode("latin-1").decode("utf-8"),
            "priority": self.headers.get("Priority", "default"),
            "body": self.rfile.read(n).decode("utf-8"),
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass  # keep pytest output clean


@pytest.fixture
def ntfy():
    # Single-threaded on purpose: requests are processed in connection
    # order, so the exact-sequence assert is free of thread-scheduling
    # reordering (fire-and-forget curls can be in flight concurrently).
    server = HTTPServer(("127.0.0.1", 0), _NtfyHandler)
    server.pushes = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def _wait(server, n):
    for _ in range(200):  # up to 10 s for fire-and-forget curls
        if len(server.pushes) >= n:
            return
        time.sleep(0.05)
    raise AssertionError(f"expected {n} pushes, got {len(server.pushes)}: "
                         f"{server.pushes}")


def test_full_lifecycle_push_sequence(ntfy, cc_home, tmp_path, fake_tmux,
                                      monkeypatch):
    port = ntfy.server_address[1]
    endpoint = f"http://127.0.0.1:{port}/topic"
    (cc_home / "config.json").write_text(
        json.dumps({"notify_enabled": True, "notify_endpoint": endpoint}),
        encoding="utf-8")

    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)      # row 1 (001) via the real hook
    _wait(ntfy, 1)
    cfg = config.load_config(cwd=repo)
    thresholds.advance(key, nnn, "WARNED", ratio=0.45, cfg=cfg)   # row 2
    thresholds.advance(key, nnn, "ROTATION_PENDING",
                       ratio=0.62, cfg=cfg)                       # row 3
    _wait(ntfy, 3)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _DRAFT, encoding="utf-8")
    checkpoint.finalize(key, nnn, cfg=cfg,
                        root=project.resolve_root(repo))          # row 4
    _wait(ntfy, 4)
    # Post-Stop shape (mirrors test_integration_phase6._setup_rotating):
    state.update_fields(key, nnn, turn_active=False,
                        last_stop_ts=time.time())
    rid = "rid-p9"
    state.transition(key, nnn, "ROTATING", rotation_id=rid)
    files = _world_payloads(tmp_path, repo, nnn)
    monkeypatch.setenv("FAKE_CLEAR_HOOK", _hook_cmd("SessionStart",
                                                    files["clear"]))
    monkeypatch.setenv("FAKE_PASTE_HOOK", _hook_cmd("UserPromptSubmit",
                                                    files["resume"]))
    rcfg = dict(config.DEFAULTS, settle_ms=0, notify_enabled=True,
                notify_endpoint=endpoint)
    rc = rotator.run(key=key, nnn=nnn, session_id=SID, pane="%1",
                     rotation_id=rid, cfg=rcfg)
    assert rc == 0, (paths.logs_dir() / "events.log").read_text()
    _wait(ntfy, 7)  # rows 1+6 (successor claim) then row 5 (rotator)
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="rotated"))
    _wait(ntfy, 8)  # row 10

    got = [(p["title"], p["priority"]) for p in ntfy.pushes]
    # issue #7 B1: warned+pending are two detached emit children fired by
    # consecutive advance() calls — python-startup jitter means no wire
    # order between the pair (same class as the 5→6 pair below).
    assert got[0] == ("▶ cc-continuity: session started", "default")  # 001
    assert got[1:3] in [
        [("⚠ cc-continuity: context warning", "default"),
         ("◆ cc-continuity: rotation pending", "high")],
        [("◆ cc-continuity: rotation pending", "high"),
         ("⚠ cc-continuity: context warning", "default")],
    ]
    assert got[3] == ("✓ cc-continuity: handoff written & verified",
                      "default")
    # P9-D7: pushes are best-effort fire-and-forget (detached curl). The
    # successor's "started" (SessionStart hook — the hook writes its
    # .started marker BEFORE firing the curl, so the rotator proceeds
    # while that curl may still be connecting) and "resumed"
    # (UserPromptSubmit hook) are two near-simultaneous deliveries with
    # NO wire-order guarantee — under load the server accepts them in
    # either order (exact-order assert went flaky 2026-09-23). The rest
    # stays exact: 5→6 is serialized by the rotator's _verify_resume
    # poll (the "resumed" curl fires inside the hook that the poll waits
    # for).
    assert got[4:6] in [
        [("▶ cc-continuity: session started", "default"),      # 002
         ("↻ cc-continuity: resumed", "default")],
        [("↻ cc-continuity: resumed", "default"),
         ("▶ cc-continuity: session started", "default")],     # 002
    ]
    assert got[6:] == [
        ("🔄 cc-continuity: rotation complete", "high"),
        ("■ cc-continuity: session ended", "default"),
    ]
    # issue #7 G1: every body now LEADS with envelope rows (proj/host/
    # model — which rows ship is machine-dependent, B3), so template rows
    # are pinned by scanning lines, not by whole-body equality.
    bodies = [p["body"] for p in ntfy.pushes]
    assert bodies[0].startswith("proj=spike-repo 001 (spike-repo-001)")
    assert "proj=spike-repo 001" in bodies[2]
    assert bodies[3].splitlines()[-1] == "proj=spike-repo 001 cp-001 sha256 OK"
    # the started-002/resumed pair may be swapped on the wire (above)
    started_i = 4 if got[4][0].endswith("session started") else 5
    assert "proj=spike-repo 002" in bodies[started_i]
    assert any(l.startswith("proj=spike-repo 001→002 ratio ")
               for l in bodies[6].splitlines())
    assert "proj=spike-repo 001" in bodies[7]


def test_signal_needs_human_reaches_server(ntfy, cc_home, tmp_path,
                                           monkeypatch):
    from cc_continuity import cli
    port = ntfy.server_address[1]
    (cc_home / "config.json").write_text(
        json.dumps({"notify_enabled": True,
                    "notify_endpoint": f"http://127.0.0.1:{port}/topic"}),
        encoding="utf-8")
    repo = make_repo(tmp_path)
    register_session(repo)  # row 1 (fire-and-forget)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)  # primary resolution
    monkeypatch.chdir(repo)
    assert cli.main(["signal", "needs-human", "drop prod db?"]) == 0
    _wait(ntfy, 2)  # row 1 + needs_human (the CLI path is wait=True)
    # row 1 is the detached emit child now (issue #7 B1): child startup
    # jitter can land it AFTER the synchronous needs_human curl — select
    # by title (P9-D7: the capture is polled, never assumed in order).
    rows = [p for p in ntfy.pushes
            if p["title"] == "🛑 cc-continuity: BLOCKED — waiting for you"]
    assert len(rows) == 1
    last = rows[0]
    assert last["priority"] == "urgent"
    assert "reason=drop prod db?" in last["body"]
