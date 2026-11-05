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
    assert b == "proj=spike-repo 003 (spike-repo-003)"  # host moved to env


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
    assert b == ("proj=spike-repo 003→004 ratio 65.0%→—\n"
                 "resumed: Run pytest -q and fix any failure.")


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
        "needs_human", slug="s", nnn=1, reason="x" * 1200, host="box")
    assert len(b) <= 1000 and "..." in b
    assert "x" * 301 not in b  # reason clipped to 300 (was 80)
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


# --- Phase 21 exit-hang event (cp-059 design, exact strings) ------------------

def test_build_message_exit_hang():
    t, b, pr = notify.build_message("exit_hang", slug="spike-repo", nnn=7,
                                    age_s=31.4, mode="kill")
    assert (t, pr) == ("💥 cc-continuity: /exit hang — killed", "high")
    assert b == "proj=spike-repo 007 /exit hung 31s — killed (kill)"


# --- Phase 22 frozen-turn escalation event (cp-063 approved design) -----------

def test_build_message_frozen_nudge():
    t, b, pr = notify.build_message("frozen_nudge", slug="spike-repo", nnn=7,
                                    frozen_minutes=23, nudge=2, max_nudges=3)
    assert (t, pr) == ("🧊 cc-continuity: frozen turn — escalated", "urgent")
    assert b == "proj=spike-repo 007 frozen 23m — ESC + nudge 2/3"


def test_build_message_frozen_nudge_picker():
    # the picker variant (Phase 22-C): frozen pane WITHOUT a turn in
    # flight — the daemon sent a single ESC, no nudge (nudge=None)
    t, b, pr = notify.build_message("frozen_nudge", slug="spike-repo",
                                    nnn=7, frozen_minutes=10)
    assert (t, pr) == ("🧊 cc-continuity: frozen pane — ESC sent", "urgent")
    assert b == "proj=spike-repo 007 frozen 10m — no turn active"


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
        ("exit_hang", dict(age_s=10 ** 6, mode="kill")),
        ("frozen_nudge", dict(frozen_minutes=10 ** 9, nudge=99,
                              max_nudges=99)),
        ("frozen_nudge", dict(frozen_minutes=10 ** 9)),  # picker variant
        ("zombie_cleaned", dict(session="cc-" + "x" * 60)),
    ]:
        _, b, _ = notify.build_message(ev, slug="s" * 40, nnn=999, **kw)
        assert len(b) <= 200, ev


# ---------------------------------------------------------------- scrubber


def test_scrub_masks_each_pattern():
    text = ("boot ok sk-ant-AP4I8fj293 done password=hunter2secret "
            "aws AKIA" + "ABCDEFGHIJKLMNOP auth bearer supersecrettoken123 "
            "cfg api_key: xyz789secret")
    out = notify.scrub(text)
    for secret in ("sk-ant-AP4I8fj293", "hunter2secret",
                   "AKIA" + "ABCDEFGHIJKLMNOP", "supersecrettoken123",
                   "xyz789secret"):
        assert secret not in out
    assert "boot ok" in out and "done" in out and "aws" in out


def test_scrub_clean_unchanged():
    text = "ratio 64% — commit 753f7e7 shipped"
    assert notify.scrub(text) == text


def test_scrub_private_key_truncates():
    text = ("summary before\n-----BEGIN RSA " "PRIVATE KEY-----\n"
            "QUJDREVG/WLJhc2U2NA==\n-----END RSA PRIVATE KEY-----\nafter")
    out = notify.scrub(text)
    assert out == "summary before"
    assert "BEGIN" not in out and "QUJD" not in out


# ---------------------------------------------------------------- envelope


def test_envelope_full():
    lines = notify.envelope(slug="claude-continuity", nnn=161,
                            name="claude-continuity-161", host="mbp",
                            branch="main", sha="4004411", dirty=True,
                            model="qwen3.8-flash-next", ratio=0.643)
    assert lines == [
        "proj=claude-continuity 161 (claude-continuity-161)",
        "host=mbp branch=main @4004411*",
        "model=qwen3.8-flash-next ratio=64.3%",
    ]


def test_envelope_omits_missing():
    lines = notify.envelope(slug="proj", nnn=3, name="proj-3",
                            host="mbp", model="opus")
    assert lines == [
        "proj=proj 003 (proj-3)",
        "host=mbp",
        "model=opus",
    ]


# ---------------------------------------------------------------- git_state


def test_git_state_parses():
    outs = iter(["main\n", "753f7e7\n", " M cc_continuity/notify.py\n"])
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs),
                                           stderr="")

    assert notify.git_state("/repo", runner=runner) == ("main", "753f7e7",
                                                        True)
    assert len(calls) == 3
    for cmd, kw in calls:
        assert cmd[:3] == ["git", "-C", "/repo"]
        assert kw.get("timeout") == 2


def test_git_state_none_on_failure():
    def raiser(cmd, **kw):
        raise OSError("no git")

    assert notify.git_state("/repo", runner=raiser) is None

    def to(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 2)

    assert notify.git_state("/repo", runner=to) is None

    def bad(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: not a repo")

    assert notify.git_state("/repo", runner=bad) is None

    def empty_branch(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    assert notify.git_state("/repo", runner=empty_branch) is None
    assert notify.git_state(None) is None


# ---------------------------------------------------------------- metric_env


def test_metric_env_lines_stale(cc_home):
    from cc_continuity import statusline
    statusline.write_metric({"session_id": "sid-2", "ts": 0,
                             "model": "opus", "ratio": 0.5})
    assert notify.metric_env_lines("sid-2") == (None, None)
    assert notify.metric_env_lines(None) == (None, None)


# ---------------------------------------------------------------- build_env


def test_build_env_full(cc_home):
    import time
    from cc_continuity import statusline
    statusline.write_metric({"session_id": "sid-1", "ts": time.time(),
                             "model": "qwen3.8-flash-next", "ratio": 0.5})
    outs = iter(["main\n", "abc1234\n", "?? x\n"])

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs),
                                           stderr="")

    lines = notify.build_env("claude-continuity", 162,
                             name="claude-continuity-162", sid="sid-1",
                             repo="/repo", host="mbp", runner=runner)
    assert lines == [
        "proj=claude-continuity 162 (claude-continuity-162)",
        "host=mbp branch=main @abc1234*",
        "model=qwen3.8-flash-next ratio=50.0%",
    ]


def test_build_env_tolerates_missing(cc_home):
    def boom(cmd, **kw):
        raise OSError("no git")

    lines = notify.build_env("proj", 3, name="proj-3", sid="nope",
                             repo="/nope", host="mbp", runner=boom)
    assert lines == ["proj=proj 003 (proj-3)", "host=mbp"]


# ---------------------------------------------------------- content fetchers


def test_last_assistant_text_picks_last(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": "go"}}),
        json.dumps({"type": "assistant", "message": {"content": "one"}}),
        json.dumps({"type": "tool_use", "name": "Bash"}),
        json.dumps({"type": "assistant", "message": {"content": "two"}}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert notify.last_assistant_text(str(p)) == "two"


def test_last_assistant_text_blocks_and_whitespace(tmp_path):
    p = tmp_path / "t.jsonl"
    rec = {"type": "assistant",
           "message": {"content": [
               {"type": "tool_use", "id": "1", "name": "Bash"},
               {"type": "text", "text": "hello\n\n  world sk-ant-AP4I8fj293"}]}}
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = notify.last_assistant_text(str(p))
    assert out == "hello world [redacted]"
    long_rec = {"type": "assistant",
                "message": {"content": "x" * 500}}
    p.write_text(json.dumps(long_rec) + "\n", encoding="utf-8")
    assert len(notify.last_assistant_text(str(p))) <= 300


def test_last_assistant_text_corrupt(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{\n", encoding="utf-8")
    assert notify.last_assistant_text(str(bad)) is None
    none = tmp_path / "none.jsonl"
    none.write_text(json.dumps(
        {"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")
    assert notify.last_assistant_text(str(none)) is None
    assert notify.last_assistant_text(str(tmp_path / "missing.jsonl")) is None
    assert notify.last_assistant_text(None) is None


def test_objective_from(tmp_path):
    h = tmp_path / "handoff.md"
    h.write_text("# Handoff\n\n## Objective\n\nShip rich lifecycle pushes.\n"
                 "More detail later.\n\n## Current Task and Exact Stopping "
                 "Point\n...\n", encoding="utf-8")
    assert notify.objective_from(str(h)) == "Ship rich lifecycle pushes."
    empty = tmp_path / "empty.md"
    empty.write_text("## Objective\n\n## Current Task\nx\n", encoding="utf-8")
    assert notify.objective_from(str(empty)) is None
    assert notify.objective_from(None) is None


# --------------------------------------------------- rich templates (issue #7)


def test_build_message_session_started_rich():
    env = ["proj=p 002 (p-2)", "host=mbp", "model=x ratio=64.2%"]
    _, b, _ = notify.build_message(
        "session_started", slug="p", nnn=2, name="p-2", env=env,
        prev="p-1", objective="Ship rich pushes sk-ant-AP4I8fj293",
        next="Task 5 GREEN")
    assert b == ("proj=p 002 (p-2)\nhost=mbp\nmodel=x ratio=64.2%\n"
                 "prev=p-1\nobjective: Ship rich pushes [redacted]\n"
                 "next: Task 5 GREEN")
    # fields absent → their lines omitted; no duplicate proj line (B3)
    _, b2, _ = notify.build_message(
        "session_started", slug="p", nnn=2, name="p-2", env=env)
    assert b2 == "proj=p 002 (p-2)\nhost=mbp\nmodel=x ratio=64.2%"


def test_build_message_stop_done():
    t, b, pr = notify.build_message(
        "stop_done", slug="proj", nnn=9, env=["proj=proj 009 (proj-9)"],
        said="Shipped. token=hunter2secret all green")
    assert (t, pr) == ("✅ cc-continuity: Claude is done", "default")
    assert b == ("proj=proj 009 (proj-9)\n"
                 "said: Shipped. key=[redacted] all green")
    # no said (content off / unreadable transcript) → still a push
    t, b, pr = notify.build_message("stop_done", slug="proj", nnn=9)
    assert b == "proj=proj 009"


def test_build_message_notification():
    t, b, pr = notify.build_message(
        "notification", slug="proj", nnn=9,
        msg="Bash(rm -rf /tmp/x) needs approval")
    assert (t, pr) == ("🔔 cc-continuity: Claude needs you", "high")
    assert b == "msg: Bash(rm -rf /tmp/x) needs approval"
    t, b, pr = notify.build_message("notification", slug="proj", nnn=9)
    assert b == "proj=proj 009"


def test_build_message_cap_1000():
    _, base, _ = notify.build_message("handoff_verified", slug="s", nnn=1)
    fill = 1000 - len(base) - 1  # env line + "\n" + event line = exactly 1000
    notify.build_message("handoff_verified", slug="s", nnn=1,
                         env=["z" * fill])
    with pytest.raises(ValueError):
        notify.build_message("handoff_verified", slug="s", nnn=1,
                             env=["z" * (fill + 1)])


def test_curl_cmd_tags_header():
    assert notify.curl_cmd("https://ntfy.sh/t", "T", "b", "high",
                           tags="rocket") == [
        "curl", "-s", "-m", "5", "-X", "POST", "https://ntfy.sh/t",
        "-H", "Title: T", "-H", "Priority: high", "-H", "Tags: rocket",
        "--data-raw", "b"]


def test_send_wait_true_passes_tags(cc_home):
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(0)

    assert notify.send("https://ntfy.sh/t", "T", "b", "default",
                       event="e", wait=True, tags="bell",
                       runner=runner) is True
    assert "Tags: bell" in seen["cmd"]


def test_safe_send_attaches_event_tags(cc_home, monkeypatch):
    fake = FakeSub()
    monkeypatch.setattr(notify, "subprocess", fake)
    assert notify.safe_send(_cfg(), "session_started", slug="p", nnn=1,
                            name="p-001") is True
    cmd = fake.popen_calls[0][0]
    assert "Tags: rocket" in cmd


def test_tags_dict_covers_all_events():
    for ev in ("session_started", "warned", "rotation_pending",
               "handoff_verified", "rotation_complete", "resumed",
               "needs_human", "error", "task_done", "session_end",
               "stop_done", "notification", "dgx_down", "dgx_up",
               "dgx_power_cycle", "turn_dead", "turn_recovered",
               "freeze_detected", "exit_hang", "frozen_nudge",
               "zombie_cleaned"):
        assert notify.TAGS[ev], ev
