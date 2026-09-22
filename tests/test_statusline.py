"""statusline.py: PRD §7 context metric + segment + chaining.

The status line is the *only* trusted context source (PRD §4 F4/F5):
``cc-continuity statusline`` reads the status line JSON on stdin, atomically
writes ``runtime/<session_id>.json``, chains the user's previous status line
command (same stdin, 2 s timeout), and appends our segment
(``⟳ 61% ok`` / ``⟳ 63% PENDING→proj-002`` / ``⟳ 73% HANDOFF!`` / ``⟳ off``).
"""

import json
import subprocess
import time

import pytest

from cc_continuity import config, paths, state, statusline

# Shape of the recorded S2 status line payload (2026-09-12, CC 2.1.209).
S2_SHAPE = {
    "session_id": "f4dcbb38-9b1a-4fa0-b76a-bf35625e09e1",
    "transcript_path": "/var/.../f4dcbb38-9b1a-4fa0-b76a-bf35625e09e1.jsonl",
    "cwd": "/Users/x/proj",
    "session_name": "s2session",
    "model": {"id": "qwen3.8-27b", "display_name": "qwen3.8-27b"},
    "workspace": {
        "current_dir": "/Users/x/proj",
        "project_dir": "/Users/x/proj",
        "added_dirs": [],
    },
    "version": "2.1.209",
    "context_window": {
        "total_input_tokens": 23170,
        "total_output_tokens": 0,
        "context_window_size": 262144,
        "current_usage": {
            "input_tokens": 23170,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "used_percentage": 9,
        "remaining_percentage": 91,
    },
}


def _payload(**overrides):
    p = json.loads(json.dumps(S2_SHAPE))
    p.update(overrides)
    return p


# ---------------------------------------------------------------- extraction


def test_used_tokens_sums_current_usage_parts(cc_home):
    p = _payload()
    p["context_window"]["current_usage"] = {
        "input_tokens": 100,
        "output_tokens": 999,  # output is NOT part of the formula
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 400,
    }
    m = statusline.extract_metric(p, window_tokens=1000)
    assert m["used_tokens"] == 700
    assert m["ratio"] == 0.7


def test_falls_back_to_total_input_when_current_usage_null(cc_home):
    p = _payload()
    p["context_window"]["current_usage"] = None
    p["context_window"]["total_input_tokens"] = 12345
    m = statusline.extract_metric(p, window_tokens=24690)
    assert m["used_tokens"] == 12345
    assert m["ratio"] == 0.5


def test_no_usable_token_data_returns_none(cc_home):
    p = _payload()
    p["context_window"]["current_usage"] = None
    p["context_window"]["total_input_tokens"] = None
    assert statusline.extract_metric(p, window_tokens=1000) is None
    # context_window missing entirely
    p2 = _payload()
    del p2["context_window"]
    assert statusline.extract_metric(p2, window_tokens=1000) is None


def test_subagent_payload_is_ignored(cc_home):
    p = _payload()
    p["agent_id"] = "agent-123"
    assert statusline.extract_metric(p, window_tokens=1000) is None


def test_missing_session_id_returns_none(cc_home):
    p = _payload()
    del p["session_id"]
    assert statusline.extract_metric(p, window_tokens=1000) is None


def test_metric_carries_prd_fields_and_records_window_size(cc_home):
    m = statusline.extract_metric(_payload(), window_tokens=262144)
    assert m["session_id"] == S2_SHAPE["session_id"]
    assert m["window_tokens"] == 262144
    assert m["cc_used_percentage"] == 9  # CC's own value, passed through
    assert m["project_dir"] == "/Users/x/proj"
    assert m["transcript_path"] == S2_SHAPE["transcript_path"]
    assert m["cc_version"] == "2.1.209"
    assert m["cc_context_window_size"] == 262144  # recorded, never trusted
    assert isinstance(m["ts"], float)
    assert 1.7e9 < m["ts"] < 2.0e9


# ---------------------------------------------------------------- writing


def test_write_metric_writes_runtime_file_atomically(cc_home):
    m = statusline.extract_metric(_payload(), window_tokens=262144)
    path = statusline.write_metric(m)
    assert path == paths.runtime_dir() / f"{m['session_id']}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == m


def test_two_sessions_get_separate_runtime_files(cc_home):
    a = statusline.extract_metric(_payload(), window_tokens=1000)
    b = statusline.extract_metric(
        _payload(session_id="other-session"), window_tokens=1000
    )
    statusline.write_metric(a)
    statusline.write_metric(b)
    assert (paths.runtime_dir() / f"{a['session_id']}.json").is_file()
    assert (paths.runtime_dir() / f"{b['session_id']}.json").is_file()


# ------------------------------------------- window_tokens default chain
# PRD §7: window_tokens = config value; default = CLAUDE_CODE_MAX_CONTEXT_TOKENS,
# else 262144.


def test_window_tokens_env_used_when_not_in_config(cc_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    assert config.load_config(cwd=tmp_path)["window_tokens"] == 200000


def test_window_tokens_env_ignored_when_config_explicit(cc_home, tmp_path, monkeypatch):
    (cc_home / "config.json").write_text(json.dumps({"window_tokens": 111111}))
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    assert config.load_config(cwd=tmp_path)["window_tokens"] == 111111


def test_window_tokens_env_invalid_falls_back_to_default(cc_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "not-a-number")
    assert config.load_config(cwd=tmp_path)["window_tokens"] == 262144


def test_window_tokens_default_without_env(cc_home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    assert config.load_config(cwd=tmp_path)["window_tokens"] == 262144


# ------------------------------------------------------- session lookup
KEY = "proj-1a2b3c4d"


def _make_session(name="proj-001", nnn=1, session_id="sid-1", state_to=None):
    state.create_session_state(KEY, name, nnn, session_id)
    if state_to:
        for s in state_to:
            state.transition(KEY, nnn, s)
    return state.load_state(KEY, nnn)


def test_find_session_by_id_returns_record(cc_home):
    rec = _make_session(session_id="abc")
    found = statusline.find_session_by_id("abc")
    assert found is not None
    assert found["nnn"] == 1
    assert found["name"] == "proj-001"


def test_find_session_by_id_across_projects(cc_home):
    _make_session(session_id="abc")
    state.create_session_state("other-11111111", "other-001", 1, "xyz")
    assert statusline.find_session_by_id("xyz")["project_key"] == "other-11111111"
    assert statusline.find_session_by_id("abc")["project_key"] == KEY


def test_find_session_by_id_unknown_returns_none(cc_home):
    _make_session(session_id="abc")
    assert statusline.find_session_by_id("nope") is None


def test_find_session_by_id_no_projects(cc_home):
    assert statusline.find_session_by_id("any") is None


# ------------------------------------------------------------ segment


def _cfg(**over):
    c = dict(config.DEFAULTS)
    c.update(over)
    return c


def test_segment_off_when_disabled(cc_home):
    assert statusline.render_segment(0.1, None, enabled=False,
                                     ansi=False) == "⟳ off"


def test_segment_off_when_session_flag_set(cc_home):
    rec = _make_session()
    rec["off"] = True
    assert statusline.render_segment(0.1, rec, enabled=True,
                                     ansi=False) == "⟳ off"


def test_segment_unknown_when_no_ratio(cc_home):
    assert statusline.render_segment(None, None, enabled=True,
                                     ansi=False) == "⟳ …"


def test_segment_ok_below_rotate(cc_home):
    rec = _make_session()  # BOOTSTRAPPING
    # ceil, so we never understate (PRD pairs ratio 0.6036 with "⟳ 61%").
    assert statusline.render_segment(0.6036, rec, enabled=True,
                                     ansi=False) == "⟳ 61% ok"
    assert statusline.render_segment(0.60, rec, enabled=True,
                                     ansi=False) == "⟳ 60% ok"


def test_segment_pending_shows_successor(cc_home):
    rec = _make_session(state_to=["ACTIVE", "WARNED", "ROTATION_PENDING"])
    assert rec["state"] == "ROTATION_PENDING"
    assert statusline.render_segment(0.63, rec, enabled=True,
                                     ansi=False) == "⟳ 63% PENDING→proj-002"


def test_segment_handoff_states(cc_home):
    for target, path in [
        ("HANDOFF_REQUESTED", ["ACTIVE", "WARNED", "ROTATION_PENDING"]),
    ]:
        state.create_session_state(KEY + "-h", "proj-001", 1, "sh")
        for s in path:
            state.transition(KEY + "-h", 1, s)
        state.transition(KEY + "-h", 1, target)
        rec = state.load_state(KEY + "-h", 1)
        assert statusline.render_segment(0.73, rec, enabled=True,
                                         ansi=False) == "⟳ 73% HANDOFF!"


def test_segment_handoff_checkpoint_states(cc_home):
    key = KEY + "-cv"
    state.create_session_state(key, "proj-001", 1, "scv")
    for s in ["ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
              "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED"]:
        state.transition(key, 1, s)
    rec = state.load_state(key, 1)
    assert statusline.render_segment(0.73, rec, enabled=True,
                                     ansi=False) == "⟳ 73% HANDOFF!"


def test_segment_none_record_ok(cc_home):
    # No matching session yet (e.g. before Phase 3 registers it): just ratio+ok.
    assert statusline.render_segment(0.5, None, enabled=True,
                                     ansi=False) == "⟳ 50% ok"


def test_segment_ceil_ignores_float_noise(cc_home):
    # 0.07 * 100 == 7.000000000000001 in IEEE 754 — naive ceil would show 8%.
    assert statusline.render_segment(0.07, None, enabled=True,
                                     ansi=False) == "⟳ 7% ok"


# ---------------------------------------------------------- chaining


def test_chain_empty_command_returns_empty(cc_home):
    assert statusline.chain(None, b"{}") == ""
    assert statusline.chain("", b"{}") == ""


def test_chain_runs_command_with_stdin_and_returns_stdout(cc_home, tmp_path):
    cmd = tmp_path / "prev.sh"
    cmd.write_text("#!/bin/sh\ncat > /dev/null\nprintf 'prev-status'\n")
    cmd.chmod(0o755)
    assert statusline.chain(str(cmd), b'{"session_id":"x"}') == "prev-status"


def test_chain_stdout_stripped_of_trailing_newline(cc_home, tmp_path):
    cmd = tmp_path / "prev.sh"
    cmd.write_text("#!/bin/sh\necho 'line'\n")
    cmd.chmod(0o755)
    assert statusline.chain(str(cmd), b"{}") == "line"


def test_chain_timeout_returns_empty(cc_home, tmp_path):
    cmd = tmp_path / "slow.sh"
    cmd.write_text("#!/bin/sh\nsleep 5\n")
    cmd.chmod(0o755)
    assert statusline.chain(str(cmd), b"{}", timeout=0.3) == ""


def test_chain_failure_returns_empty(cc_home, tmp_path):
    cmd = tmp_path / "boom.sh"
    cmd.write_text("#!/bin/sh\nexit 3\n")
    cmd.chmod(0o755)
    assert statusline.chain(str(cmd), b"{}") == ""


def test_chain_missing_command_returns_empty(cc_home):
    assert statusline.chain("/nonexistent/xyz-123", b"{}") == ""


# --------------------------------------------------------- compose/run


def test_compose_line_appends_segment_after_chained(cc_home):
    assert statusline.compose_line("prev", "⟳ 61% ok") == "prev ⟳ 61% ok"
    assert statusline.compose_line("", "⟳ 61% ok") == "⟳ 61% ok"


def test_run_writes_metric_and_renders_segment(cc_home, tmp_path, monkeypatch):
    payload = _payload()
    line = statusline.run(json.dumps(payload).encode(),
                          _cfg(statusline_ansi=False))
    assert line == "⟳ 9% ok"  # 23170/262144 -> 8.8% -> ceil 9
    m = json.loads(
        (paths.runtime_dir() / f"{payload['session_id']}.json").read_text()
    )
    assert m["used_tokens"] == 23170
    assert m["ratio"] == pytest.approx(23170 / 262144, abs=1e-4)


def test_run_chains_user_statusline(cc_home, tmp_path):
    prev = tmp_path / "prev.sh"
    prev.write_text("#!/bin/sh\nprintf 'hud'\n")
    prev.chmod(0o755)
    payload = _payload()
    line = statusline.run(json.dumps(payload).encode(),
                          _cfg(chained_statusline=str(prev), statusline_ansi=False))
    assert line == "hud ⟳ 9% ok"


def test_run_disabled_writes_nothing_and_shows_off(cc_home, monkeypatch):
    payload = _payload()
    line = statusline.run(json.dumps(payload).encode(),
                          _cfg(enabled=False, statusline_ansi=False))
    assert line == "⟳ off"
    assert not (paths.runtime_dir() / f"{payload['session_id']}.json").exists()


def test_run_invalid_stdin_does_not_crash(cc_home):
    line = statusline.run(b"this is not json", _cfg(statusline_ansi=False))
    # No metric, no session, ratio unknown.
    assert line == "⟳ …"


# ------------------------------------------------------- read + staleness


def test_read_metric_returns_written_metric(cc_home):
    m = statusline.extract_metric(_payload(), window_tokens=262144)
    statusline.write_metric(m)
    assert statusline.read_metric(m["session_id"]) == m


def test_read_metric_missing_or_corrupt_returns_none(cc_home):
    assert statusline.read_metric("never-written") is None
    bad = paths.runtime_dir() / "corrupt.json"
    bad.write_text("{not json")
    assert statusline.read_metric("corrupt") is None


def test_is_stale_missing_metric(cc_home):
    assert statusline.is_stale(None) is True


def test_is_stale_fresh_metric(cc_home):
    m = {"ts": time.time()}
    assert statusline.is_stale(m) is False


def test_is_stale_older_than_ten_minutes(cc_home):
    m = {"ts": time.time() - 601}
    assert statusline.is_stale(m) is True
    m2 = {"ts": time.time() - 599}
    assert statusline.is_stale(m2) is False


def test_render_segment_rotating_form(cc_home):
    assert statusline.render_segment(0.7, {"name": "proj-001",
                                            "state": "ROTATING"}, True,
                                     ansi=False) \
        == "⟳ 70% ROTATING"


def test_render_segment_amcl_form(cc_home):
    assert statusline.render_segment(0.7, {"name": "proj-001",
                                            "state": "AWAITING_MANUAL_CLEAR"}, True,
                                     ansi=False) \
        == "⟳ 70% AMCL"


def test_render_segment_cv_stays_handoff(cc_home):
    assert statusline.render_segment(0.7, {"name": "proj-001",
                                            "state": "CHECKPOINT_VERIFIED"}, True,
                                     ansi=False) \
        == "⟳ 70% HANDOFF!"


# ------------------------------------------------- Phase 9: ⛔ BLOCKED


def test_render_segment_blocked_suffix():
    rec = {"state": "ACTIVE", "name": "spike-repo-001"}
    assert statusline.render_segment(0.5, rec, True, ansi=False) == "⟳ 50% ok"
    assert statusline.render_segment(0.5, rec, True,
                                     blocked="drop db?", ansi=False) == \
        "⟳ 50% ok ⛔ BLOCKED"
    assert statusline.render_segment(None, rec, True,
                                     blocked="x", ansi=False) == "⟳ … ⛔ BLOCKED"
    assert statusline.render_segment(0.5, rec, False,
                                     blocked="x", ansi=False) == "⟳ off"


def test_run_shows_blocked_when_needs_human_pending(cc_home, tmp_path):
    from cc_continuity import ledger
    from hookutil import SID, make_repo, register_session
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "needs_human", "reason": "drop db?"})
    payload = {
        "session_id": SID,
        "cwd": str(repo),
        "context_window": {
            "context_window_size": 262144,
            "current_usage": {
                "input_tokens": 131072,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    # Phase 11: colored by default — pin the contract in plain mode
    # (the ANSI BLOCKED span is asserted in the Phase 11 block below).
    line = statusline.run(json.dumps(payload).encode("utf-8"),
                          dict(config.DEFAULTS, statusline_ansi=False))
    assert line == "⟳ 50% ok ⛔ BLOCKED"


# ----------------------------------- Phase 11: color + shimmer sweep

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
# (base, shimmer) basic-16 SGR pairs — normal + bright variant (the
# ultrathink.sh concept, palette chosen for 16-color-client survival)
_G, _GS = "\x1b[32m", "\x1b[92m"  # green
_Y, _YS = "\x1b[33m", "\x1b[93m"  # yellow
_B, _BS = "\x1b[36m", "\x1b[96m"  # cyan
_V, _VS = "\x1b[35m", "\x1b[95m"  # magenta
_R, _RS = "\x1b[31m", "\x1b[91m"  # red


def _colored(text: str, base: str, bright: str | None = None,
             band: set = frozenset()) -> str:
    """Per-char escapes (ultrathink.sh loop); ``band`` = shimmer indices."""
    bright = bright or base
    return _BOLD + "".join(
        (bright if i in band else base) + ch for i, ch in enumerate(text)) \
        + _RESET


def test_shimmer_index_math():
    # 50 ms tick; length 8 -> cycle 28 frames; 10-cell rest each side
    assert statusline.shimmer_index(0.0, 8) == -10
    assert statusline.shimmer_index(0.57, 8) == 1   # frame 11
    assert statusline.shimmer_index(0.62, 8) == 2   # frame 12
    assert statusline.shimmer_index(0.67, 8) == 3   # frame 13
    # one full cycle (28 ticks = 1.4 s) lands on the same cell
    assert statusline.shimmer_index(0.62 + 1.4, 8) == 2


def test_segment_ansi_shimmer_band_three_chars():
    rec = {"state": "ACTIVE", "name": "p-001"}
    text = "⟳ 61% ok"
    # rest: glimmer parked 10 cells left -> all base
    assert statusline.render_segment(0.61, rec, True, now=0.0) == \
        _colored(text, _G)
    # glimmer=2 -> chars 1..3 (space, '6', '1') in the bright variant
    assert statusline.render_segment(0.61, rec, True, now=0.62) == \
        _colored(text, _G, _GS, band={1, 2, 3})


def test_segment_ansi_state_colors():
    cases = [
        (0.63, {"name": "p-001", "state": "ROTATION_PENDING"},
         "⟳ 63% PENDING→p-002", _Y),
        (0.7, {"name": "p-001", "state": "ROTATING"}, "⟳ 70% ROTATING", _B),
        (0.7, {"name": "p-001", "state": "AWAITING_MANUAL_CLEAR"},
         "⟳ 70% AMCL", _V),
        (0.73, {"name": "p-001", "state": "HANDOFF_REQUESTED"},
         "⟳ 73% HANDOFF!", _R),
    ]
    for ratio, rec, text, base in cases:
        assert statusline.render_segment(ratio, rec, True, now=0.0) == \
            _colored(text, base)


def test_segment_ansi_blocked_span_own_color():
    rec = {"state": "ACTIVE", "name": "p-001"}
    span = _BOLD + _R + " ⛔ BLOCKED" + _RESET
    assert statusline.render_segment(0.5, rec, True, blocked="x", now=0.0) \
        == _colored("⟳ 50% ok", _G) + span
    # the band sweeps the segment only — the BLOCKED span stays put
    assert statusline.render_segment(0.5, rec, True, blocked="x", now=0.62) \
        .endswith(span)


def test_segment_ansi_off_and_unknown_are_static_gray():
    off = "\x1b[90m⟳ off" + _RESET
    unknown = "\x1b[90m⟳ …" + _RESET
    rec_off = {"state": "ACTIVE", "name": "p-001", "off": True}
    for now in (0.0, 0.62, 1.0):  # static: never shimmers
        assert statusline.render_segment(0.1, None, False, now=now) == off
        assert statusline.render_segment(0.1, rec_off, True, now=now) == off
        assert statusline.render_segment(None, None, True, now=now) == unknown


def test_segment_plain_mode_byte_identical_when_ansi_false():
    rec = {"state": "ACTIVE", "name": "p-001"}
    assert statusline.render_segment(0.5, rec, True, blocked="x",
                                     ansi=False) == "⟳ 50% ok ⛔ BLOCKED"
    assert statusline.render_segment(None, rec, True, blocked="x",
                                     ansi=False) == "⟳ … ⛔ BLOCKED"
    assert statusline.render_segment(None, None, True,
                                     ansi=False) == "⟳ …"
    # plain mode emits no escape codes
    assert "\x1b" not in statusline.render_segment(0.5, rec, True,
                                                   blocked="x", ansi=False)


def test_statusline_ansi_default_true():
    assert config.DEFAULTS["statusline_ansi"] is True


def test_run_ansi_off_renders_legacy_plain_line(cc_home, tmp_path):
    payload = _payload()
    line = statusline.run(json.dumps(payload).encode(),
                          _cfg(statusline_ansi=False))
    assert line == "⟳ 9% ok"


def test_run_ansi_on_renders_shimmer_segment(cc_home, tmp_path, monkeypatch):
    payload = _payload()
    monkeypatch.setattr(statusline.time, "time", lambda: 0.0)  # rest
    line = statusline.run(json.dumps(payload).encode(), _cfg())
    assert line == _colored("⟳ 9% ok", _G)
    # glimmer=1 -> chars 0..2 ('⟳', ' ', '9') in the bright variant
    monkeypatch.setattr(statusline.time, "time", lambda: 0.55)
    line = statusline.run(json.dumps(payload).encode(), _cfg())
    assert line == _colored("⟳ 9% ok", _G, _GS, band={0, 1, 2})
