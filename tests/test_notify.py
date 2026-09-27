"""notify.py: Phase 9 best-effort ntfy pushes (spec P9-D1/D4/D7, §5)."""

import json
import subprocess

import pytest

from cc_continuity import config, notify, paths


def _cfg(**over):
    cfg = dict(config.DEFAULTS)
    cfg.update({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic",
    })
    cfg.update(over)
    return cfg


class FakeProc:
    def __init__(self, rc=0):
        self.returncode = rc


class FakeSub:
    """Stands in for the subprocess module inside notify."""
    def __init__(self):
        self.popen_calls = []
        self.run_calls = []

    def Popen(self, cmd, **kw):
        self.popen_calls.append((cmd, kw))
        return object()

    def run(self, cmd, **kw):
        self.run_calls.append((cmd, kw))
        return FakeProc(0)


# --- enabled() ----------------------------------------------------------------

def test_enabled_requires_master_switch_and_flag_and_endpoint(monkeypatch):
    assert notify.enabled({}) is False
    assert notify.enabled(_cfg(notify_enabled=False)) is False
    assert notify.enabled(_cfg(notify_endpoint="")) is False
    assert notify.enabled(_cfg()) is True
    monkeypatch.setenv("CC_CONTINUITY", "0")
    assert notify.enabled(_cfg()) is False


# --- build_message(): exact spec §5 strings -----------------------------------

def test_build_session_started():
    t, b, p = notify.build_message(
        "session_started", slug="spike-repo", nnn=3,
        name="spike-repo-003", host="box")
    assert (t, p) == ("▶ cc-continuity: session started", "default")
    assert b == "proj=spike-repo 003 (spike-repo-003) host=box"


def test_build_warned_and_pending():
    t, b, p = notify.build_message(
        "warned", slug="spike-repo", nnn=3, ratio=0.58, rotate_threshold=0.62)
    assert (t, p) == ("⚠ cc-continuity: context warning", "default")
    assert b == "proj=spike-repo 003 ratio=58.0% — rotating at 62%"
    t, b, p = notify.build_message(
        "rotation_pending", slug="spike-repo", nnn=3, ratio=0.65)
    assert (t, p) == ("◆ cc-continuity: rotation pending", "high")
    assert b == ("proj=spike-repo 003 ratio=65.0% — no interruption; "
                 "rotates at turn end")
    # stale/unknown ratio renders as an em-dash, never crashes
    _, b2, _ = notify.build_message("warned", slug="s", nnn=1, ratio=None,
                                    rotate_threshold=0.62)
    assert "ratio=—" in b2


def test_build_handoff_verified_resumed_session_end():
    t, b, p = notify.build_message("handoff_verified", slug="s", nnn=4)
    assert (t, p) == ("✓ cc-continuity: handoff written & verified", "default")
    assert b == "proj=s 004 cp-004 sha256 OK"
    t, b, p = notify.build_message("resumed", slug="s", nnn=5)
    assert (t, p) == ("↻ cc-continuity: resumed", "default")
    assert b == "proj=s 005 VALIDATION OK — continuing from NAA"
    t, b, p = notify.build_message("session_end", slug="s", nnn=4,
                                   reason="clear")
    assert (t, p) == ("■ cc-continuity: session ended", "default")
    assert b == "proj=s 004 reason=clear"


def test_build_rotation_complete():
    t, b, p = notify.build_message(
        "rotation_complete", slug="spike-repo", nnn=3, nnn_new=4,
        r0=0.65, r1=None, naa="Run pytest -q and fix any failure.")
    assert (t, p) == ("🔄 cc-continuity: rotation complete", "high")
    assert b == ("proj=spike-repo 003→004 ratio 65.0%→— resumed: "
                 "Run pytest -q and fix any failure.")


def test_build_needs_human_urgent_and_error():
    t, b, p = notify.build_message(
        "needs_human", slug="s", nnn=7, reason="drop production db?",
        host="box")
    assert t == "🛑 cc-continuity: BLOCKED — waiting for you"
    assert p == "urgent"
    assert b == ("proj=s 007 reason=drop production db? host=box — "
                 "remote in and reply in session 007")
    t, b, p = notify.build_message(
        "error", slug="s", nnn=3, what="rotation",
        detail="no RESUME in successor ledger")
    assert t == "✗ cc-continuity: rotation failed"
    assert p == "urgent"
    assert b.startswith("proj=s 003 detail=no RESUME in successor ledger — see ")
    assert b.endswith("/errors.log")


def test_build_task_done():
    t, b, p = notify.build_message("task_done", slug="s", nnn=0,
                                   summary="all 14 tasks done")
    assert (t, p) == ("✅ cc-continuity: task complete", "default")
    assert b == "proj=s summary=all 14 tasks done"


def test_build_clips_long_fields_and_rejects_unknown_event():
    t, b, p = notify.build_message(
        "needs_human", slug="s", nnn=1, reason="x" * 300, host="box")
    assert len(b) <= 200 and "..." in b
    with pytest.raises(ValueError):
        notify.build_message("nope", slug="s", nnn=1)


# --- naa_snippet ---------------------------------------------------------------

def test_naa_snippet(tmp_path):
    f = tmp_path / "handoff.md"
    f.write_text("## Next Atomic Action\n\nRun `pytest -q` and fix any failure "
                 "that appears.\n\n## Remaining Plan\nx\n", encoding="utf-8")
    assert notify.naa_snippet(str(f), 60) == \
        "Run `pytest -q` and fix any failure that appears."
    assert notify.naa_snippet(str(tmp_path / "missing.md")) == ""


# --- send(): wire form + failure logging ---------------------------------------

def test_curl_cmd_wire_form():
    assert notify.curl_cmd("https://ntfy.sh/t", "T", "b", "high") == [
        "curl", "-s", "-m", "5", "-X", "POST", "https://ntfy.sh/t",
        "-H", "Title: T", "-H", "Priority: high", "--data-raw", "b"]


def test_send_wait_true_failure_logged(cc_home, monkeypatch):
    fake = FakeSub()
    fake.run = lambda cmd, **kw: FakeProc(7)  # type: ignore[method-assign]
    assert notify.send("https://ntfy.sh/t", "T", "b", "default",
                       event="test-ev", wait=True,
                       runner=lambda cmd, **kw: FakeProc(7)) is False
    log = (paths.logs_dir() / "errors.log").read_text(encoding="utf-8")
    assert "notify: test-ev failed: curl exit 7" in log


def test_send_wait_true_timeout_logged(cc_home):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=6)
    assert notify.send("https://ntfy.sh/t", "T", "b", "default",
                       event="tmo", wait=True, runner=boom) is False
    log = (paths.logs_dir() / "errors.log").read_text(encoding="utf-8")
    assert "notify: tmo failed: TimeoutExpired" in log


def test_send_wait_false_is_detached_popen(cc_home, monkeypatch):
    fake = FakeSub()
    monkeypatch.setattr(notify, "subprocess", fake)
    assert notify.send("https://ntfy.sh/t", "T", "b", "default",
                       event="e") is True
    (cmd, kw) = fake.popen_calls[0]
    assert cmd[:3] == ["curl", "-s", "-m"]
    assert kw["start_new_session"] is True


def _mk_session(key: str, nnn: int, monkeypatch=None) -> None:
    from cc_continuity import state as st
    st.create_session_state(key, f"proj-{key}-{nnn:03d}", nnn,
                            f"sid-{key}", initial_state="ACTIVE")


def test_advance_fires_row2_then_row3(cc_home, monkeypatch):
    from cc_continuity import thresholds
    _mk_session("k1", 1)
    fired = []
    monkeypatch.setattr(notify, "safe_send",
                        lambda cfg, event, **kw: fired.append(event) or True)
    thresholds.advance("k1", 1, "WARNED", ratio=0.5, cfg=_cfg())
    thresholds.advance("k1", 1, "HANDOFF_REQUESTED", ratio=0.65, cfg=_cfg())
    assert fired == ["warned", "rotation_pending"]


def test_advance_midwalk_still_fires_pending(cc_home, monkeypatch):
    """P9-P3: a direct walk to HANDOFF_REQUESTED passes THROUGH
    ROTATION_PENDING — row 3 must fire even though no caller asked for it."""
    from cc_continuity import thresholds
    _mk_session("k2", 1)
    fired = []
    monkeypatch.setattr(notify, "safe_send",
                        lambda cfg, event, **kw: fired.append(event) or True)
    thresholds.advance("k2", 1, "HANDOFF_REQUESTED", ratio=0.75, cfg=_cfg())
    assert fired == ["rotation_pending"]


def test_advance_without_cfg_is_silent(cc_home, monkeypatch):
    from cc_continuity import thresholds
    _mk_session("k3", 1)
    fired = []
    monkeypatch.setattr(notify, "safe_send",
                        lambda cfg, event, **kw: fired.append(event) or True)
    thresholds.advance("k3", 1, "ROTATION_PENDING", ratio=0.65)
    assert fired == []


# --- Phase 10 watchdog events (spec P10-D11, exact strings) ------------------


def test_build_message_dgx_down():
    t, b, pr = notify.build_message("dgx_down", slug="", nnn=0,
                                    host="mac-mini", interval_min=5)
    assert (t, pr) == ("🔌 cc-continuity: model server DOWN", "urgent")
    assert b == "host=mac-mini — probing every 5 min; nudges deferred"


def test_build_message_dgx_up_formats_downtime():
    # 3930 s = 1h 05m 30s → floors to "1h 05m" (zero-padded minutes)
    t, b, pr = notify.build_message("dgx_up", slug="", nnn=0,
                                    downtime_s=3930)
    assert (t, pr) == ("🔌 cc-continuity: model server back", "high")
    assert b == "down for 1h 05m — nudges resume"


def test_build_message_turn_dead_first_nudge():
    t, b, pr = notify.build_message("turn_dead", slug="spike-repo", nnn=7,
                                    idle_minutes=47, max_nudges=3)
    assert (t, pr) == ("⟲ cc-continuity: turn died — recovering", "high")
    assert b == "proj=spike-repo 007 idle 47m — nudge 1/3"


def test_build_message_turn_recovered():
    t, b, pr = notify.build_message("turn_recovered", slug="spike-repo",
                                    nnn=7, nudge=2)
    assert (t, pr) == ("✅ cc-continuity: turn recovered", "default")
    assert b == "proj=spike-repo 007 nudge 2 verified (user_prompt)"


def test_build_message_freeze_detected():
    t, b, pr = notify.build_message("freeze_detected",
                                    slug="spike-repo", nnn=7)
    assert (t, pr) == ("🧊 cc-continuity: retry-loop freeze?", "high")
    assert b == "proj=spike-repo 007 pane stuck, input closed — no auto-fix"


def test_fmt_minutes():
    assert notify._fmt_minutes(0) == "0m"
    assert notify._fmt_minutes(59 * 60) == "59m"
    assert notify._fmt_minutes(3930) == "1h 05m"
    assert notify._fmt_minutes(90 * 60) == "1h 30m"


def test_watchdog_bodies_under_200_chars():
    # the 200-char cap (build_message) must hold even for extreme fields
    for ev, kw in [
        ("dgx_down", dict(host="a" * 60, interval_min=60)),
        ("dgx_up", dict(downtime_s=10 ** 6)),
        ("turn_dead", dict(idle_minutes=10 ** 9, max_nudges=99)),
        ("turn_recovered", dict(nudge=99)),
        ("freeze_detected", {}),
    ]:
        _, b, _ = notify.build_message(ev, slug="s" * 40, nnn=999, **kw)
        assert len(b) <= 200, ev
