"""thresholds.py: ratio readers, C-texts, allowlist, pct (spec §4)."""

import time

from cc_continuity import config, paths, statusline, thresholds
from cc_continuity.hooks import common

SID = "11111111-2222-3333-4444-555555555555"
DRAFT = "/home/x/.claude-continuity/projects/p-1234/sessions/001/handoff.draft.md"


def _ctx():
    session = {"session_id": SID, "state": "ACTIVE",
               "project_key": "p-1234", "nnn": 1}
    return common.HookContext("PostToolUse", {"session_id": SID},
                              dict(config.DEFAULTS), session)


def _write_metric(ratio, age=0.0):
    statusline.write_metric({"session_id": SID, "ts": time.time() - age,
                             "used_tokens": 1, "window_tokens": 100,
                             "ratio": ratio})


def test_pct_of_uses_statusline_convention(cc_home):
    assert thresholds.pct_of(0.07) == 7
    assert thresholds.pct_of(0.6036) == 61
    assert thresholds.pct_of(0.72) == 72


def test_c1_text(cc_home):
    t = thresholds.c1_text(0.56, 262144)
    assert t.startswith("CONTINUITY: context at 56% of 262144.")
    assert "rotation will be needed soon" in t
    assert "clean stopping point" in t


def test_c2_text_lists_draft_and_headings(cc_home):
    t = thresholds.c2_text(0.63, DRAFT)
    assert "Rotation pending" in t
    assert f"write the handoff to {DRAFT}" in t
    assert "Current Task and Exact Stopping Point" in t
    assert "Rejected Approaches — Do Not Repeat" in t


def test_c3_text_falls_back_to_hard_ratio_pct(cc_home):
    assert "context at 72% (hard limit)" in thresholds.c3_text(None, 0.72, DRAFT)
    assert "context at 75% (hard limit)" in thresholds.c3_text(0.75, 0.72, DRAFT)
    assert "Tool blocked" in thresholds.c3_text(0.75, 0.72, DRAFT)


def test_c6_and_c9_exact(cc_home):
    assert thresholds.c6_text(["missing heading: X"], DRAFT) == (
        "CONTINUITY: handoff draft invalid: missing heading: X. Fix "
        + DRAFT + " and end your turn.")
    assert thresholds.c9_text() == (
        "CONTINUITY: handoff accepted. Do not start any more work in this "
        "session. End your turn now.")


def test_read_ratio_fresh(cc_home):
    _write_metric(0.61)
    assert thresholds.read_ratio(_ctx()) == 0.61


def test_read_ratio_stale_returns_none_and_logs(cc_home):
    _write_metric(0.61, age=700)
    assert thresholds.read_ratio(_ctx()) is None
    log = (paths.continuity_home() / "logs" / "events.log").read_text()
    assert "METRIC_STALE" in log and SID in log


def test_read_ratio_missing(cc_home):
    assert thresholds.read_ratio(_ctx()) is None


def test_display_ratio_prefers_fresh(cc_home):
    _write_metric(0.61)
    assert thresholds.display_ratio(_ctx(), config.DEFAULTS) == 0.61


def test_display_ratio_falls_back_to_stale_then_hard(cc_home):
    _write_metric(0.59, age=700)
    assert thresholds.display_ratio(_ctx(), config.DEFAULTS) == 0.59
    (paths.runtime_dir() / f"{SID}.json").unlink()
    assert thresholds.display_ratio(_ctx(), config.DEFAULTS) == 0.72


def test_is_allowed_tool_matrix(cc_home):
    assert thresholds.is_allowed_tool("Read", {}, DRAFT)
    assert thresholds.is_allowed_tool("Glob", {}, DRAFT)
    assert thresholds.is_allowed_tool("Grep", {}, DRAFT)
    assert thresholds.is_allowed_tool("TaskUpdate", {"taskId": "1"}, DRAFT)
    assert thresholds.is_allowed_tool("TaskCreate", {}, DRAFT)
    assert thresholds.is_allowed_tool("Write", {"file_path": DRAFT}, DRAFT)
    assert thresholds.is_allowed_tool("Edit", {"file_path": DRAFT}, DRAFT)
    assert not thresholds.is_allowed_tool("Write", {"file_path": "/a.py"}, DRAFT)
    assert not thresholds.is_allowed_tool("Edit", {"file_path": "/a.py"}, DRAFT)
    assert thresholds.is_allowed_tool("Bash", {"command": "git status"}, DRAFT)
    assert thresholds.is_allowed_tool("Bash", {"command": "git diff --stat"}, DRAFT)
    assert thresholds.is_allowed_tool("Bash", {"command": "git rev-parse HEAD"}, DRAFT)
    assert thresholds.is_allowed_tool("Bash", {"command": "cc-continuity status"}, DRAFT)
    assert not thresholds.is_allowed_tool("Bash", {"command": "git commit -m x"}, DRAFT)
    assert not thresholds.is_allowed_tool("Bash", {"command": "pytest -q"}, DRAFT)
    assert not thresholds.is_allowed_tool("Bash", {"command": ""}, DRAFT)
    assert not thresholds.is_allowed_tool("WebFetch", {"url": "x"}, DRAFT)
    assert not thresholds.is_allowed_tool(None, {}, DRAFT)


# ------------------------------------------------------------- advance (Task 3)

import pytest

from cc_continuity import ledger, state
from hookutil import make_repo, project_key_for


def _mk(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key = project_key_for(repo)
    state.create_session_state(key, "repo-001", 1, "sid-a")
    return key


def _to(key, *chain):
    for s in chain:
        state.transition(key, 1, s)


def test_advance_active_to_pending_sets_fields(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    _to(key, "ACTIVE")
    rec, changed = thresholds.advance(key, 1, "ROTATION_PENDING", ratio=0.62)
    assert changed is True
    assert rec["state"] == "ROTATION_PENDING"
    assert rec["warned"] is True
    assert rec["ratio_at_pending"] == 0.62
    ev = ledger.read(paths.session_dir(key, 1))[-1]
    assert ev == {"type": "state", "from": "ACTIVE", "to": "ROTATION_PENDING",
                  "fields": {"warned": True, "ratio_at_pending": 0.62},
                  "ts": ev["ts"]}


def test_advance_bootstrap_to_handoff_walks_full_path(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)  # created in BOOTSTRAPPING
    rec, changed = thresholds.advance(key, 1, "HANDOFF_REQUESTED", ratio=0.73)
    assert changed is True and rec["state"] == "HANDOFF_REQUESTED"
    walk = [(e["from"], e["to"]) for e in ledger.read(paths.session_dir(key, 1))
            if e["type"] == "state"]
    assert walk == [("BOOTSTRAPPING", "ACTIVE"),
                    ("ACTIVE", "ROTATION_PENDING"),
                    ("ROTATION_PENDING", "HANDOFF_REQUESTED")]


def test_advance_warned_to_handoff_via_pending(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    _to(key, "ACTIVE", "WARNED")
    rec, _ = thresholds.advance(key, 1, "HANDOFF_REQUESTED")
    assert rec["state"] == "HANDOFF_REQUESTED"


def test_advance_pending_to_handoff_direct(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    _to(key, "ACTIVE", "ROTATION_PENDING")
    rec, _ = thresholds.advance(key, 1, "HANDOFF_REQUESTED")
    assert rec["state"] == "HANDOFF_REQUESTED"


def test_advance_idempotent_noop(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    _to(key, "ACTIVE", "ROTATION_PENDING")
    rec, changed = thresholds.advance(key, 1, "ROTATION_PENDING")
    assert changed is False and rec["state"] == "ROTATION_PENDING"


def test_advance_rejects_bad_targets_and_sources(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    with pytest.raises(state.InvalidTransitionError):
        thresholds.advance(key, 1, "CHECKPOINT_VERIFIED")
    _to(key, "ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED")
    with pytest.raises(state.InvalidTransitionError):
        thresholds.advance(key, 1, "ROTATION_PENDING")  # backwards: unreachable
    _to(key, "CHECKPOINT_WRITING")
    with pytest.raises(state.InvalidTransitionError):
        thresholds.advance(key, 1, "HANDOFF_REQUESTED")  # CW -> HR illegal


def test_advance_ratio_none_keeps_ratio_at_pending(cc_home, tmp_path):
    key = _mk(cc_home, tmp_path)
    _to(key, "ACTIVE")
    thresholds.advance(key, 1, "ROTATION_PENDING", ratio=0.65)
    thresholds.advance(key, 1, "HANDOFF_REQUESTED")  # ratio=None
    assert state.load_state(key, 1)["ratio_at_pending"] == 0.65
