"""statusline.py: PRD §7 context metric + segment + chaining.

The status line is the *only* trusted context source (PRD §4 F4/F5):
``cc-continuity statusline`` reads the status line JSON on stdin, atomically
writes ``runtime/<session_id>.json``, chains the user's previous status line
command (same stdin, 2 s timeout), and appends our segment
(``⟳ 61% ok`` / ``⟳ 63% PENDING→proj-002`` / ``⟳ 73% HANDOFF!`` / ``⟳ off``).
"""

import json
import os
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
_REVERSE = "\x1b[7m"  # what the band must NOT contain (rev 8+ contract)
# Revision 9 (2026-09-23): the band = the BRIGHTER SAME-HUE 24-bit
# SHIMMER variant of each char's own base hue, nothing else. Hue shift
# is REJECTED (the 15:32 video: a shifted hue on a rainbow has no
# identity — "ne oldugu belirsiz, cooook agir"), 9x bright is
# theme-eaten (020 BRIGHT TEST), reverse video read as corrupted text
# ("okunmuyor bile"). The 24-bit port is proven on the user's chain:
# tmux 3.7b client emits 38;2 SGR for hex styles (pty capture, 09-23)
# and the user's eyes read the app's own 24-bit input-box rainbow as
# "renkli" (the 09-22 "gray" was the old 0a9b client — different era).
# Per-character palettes (ultrathink.sh, verbatim), cycled ``i % 7``:
_RAINBOW = [  # BASE — per-char static
    "\x1b[38;2;235;95;87m",   # red
    "\x1b[38;2;245;139;87m",  # orange
    "\x1b[38;2;250;195;95m",  # yellow
    "\x1b[38;2;145;200;130m", # green
    "\x1b[38;2;130;170;220m", # blue
    "\x1b[38;2;155;130;200m", # indigo
    "\x1b[38;2;200;130;180m", # violet
]
_SHIMMER = [  # BRIGHTER SAME-HUE band variant
    "\x1b[38;2;250;155;147m",  # red_shimmer
    "\x1b[38;2;255;185;137m",  # orange_shimmer
    "\x1b[38;2;255;225;155m",  # yellow_shimmer
    "\x1b[38;2;185;230;180m",  # green_shimmer
    "\x1b[38;2;180;205;240m",  # blue_shimmer
    "\x1b[38;2;195;180;230m",  # indigo_shimmer
    "\x1b[38;2;230;180;210m",  # violet_shimmer
]
_R = "\x1b[31m"  # red — the BLOCKED suffix keeps its own span


def _rainbow(text: str, band: set = frozenset()) -> str:
    """Per-char 24-bit rainbow escapes (ultrathink.sh loop); ``band`` =
    the shimmer indices. Char ``i`` uses ``_RAINBOW[i % 7]``; band chars
    (revision 9) use ``_SHIMMER[i % 7]`` — the BRIGHTER SAME-HUE variant
    (luminance contrast, the axis the user's chain actually shows). NO
    hue shift, NO reverse video, NO 9x bright."""
    return _BOLD + "".join(
        ((_SHIMMER[i % len(_SHIMMER)]
          if i in band
          else _RAINBOW[i % len(_RAINBOW)]) + ch)
        for i, ch in enumerate(text)) + _RESET


def test_shimmer_index_math():
    # 50 ms tick (revision 9 = the real CC cadence, 3x faster than the
    # 150 ms the user read as "cooook agir"), NO rest frame (revision 5):
    # glimmer always in [0, length)
    assert statusline.shimmer_index(0.0, 8) == 0
    assert statusline.shimmer_index(0.06, 8) == 1
    assert statusline.shimmer_index(0.11, 8) == 2
    assert statusline.shimmer_index(0.26, 8) == 5
    # one full cycle (8 ticks = 0.4 s) lands on the same cell
    assert statusline.shimmer_index(0.26 + 0.4, 8) == 5


def test_shimmer_band_always_visible_no_rest_frame():
    # revision 5: EVERY render carries the band — the rest frame that left
    # it off on ~64% of renders was the "shimmering" flicker (2026-09-22)
    rec = {"state": "ACTIVE", "name": "p-001"}
    for now in (0.0, 0.05, 0.1, 0.17, 0.4, 1.0, 2.5):
        out = statusline.render_segment(0.61, rec, True, now=now,
                                        shimmer=True)
        # rev 8: band present on EVERY render = at least one char re-hued
        # (2 chars min at a text edge); no rest frame
        static = statusline.render_segment(0.61, rec, True, now=now,
                                           shimmer=False)
        assert out != static, now


def test_segment_ansi_shimmer_band_three_chars():
    rec = {"state": "ACTIVE", "name": "p-001"}
    text = "⟳ 61% ok"
    # default = static rainbow (no band)
    assert statusline.render_segment(0.61, rec, True, now=0.0) == \
        _rainbow(text)
    # shimmer on, now=0.62 -> glimmer = floor(0.62/0.05) % 8 = 4 ->
    # chars 3..5 ('6', '1', '%') in the brighter same-hue variant
    assert statusline.render_segment(0.61, rec, True, now=0.62,
                                     shimmer=True) == \
        _rainbow(text, band={3, 4, 5})


def test_shimmer_band_uses_brighter_same_hue_not_9x_not_reverse():
    # revision 9 (2026-09-23): the band is the BRIGHTER SAME-HUE 24-bit
    # shimmer variant of the char's own base — NO 9x bright (theme-eaten
    # on the user's display chain, 020 BRIGHT TEST), NO reverse video
    # ("okunmuyor bile", 13:53:5x), NO hue shift (the 15:32 video:
    # identity-less drift, "ne oldugu belirsiz, cooook agir").
    # now=0.62 → glimmer 4 → band {3,4,5}: '1' green→green_shimmer,
    # '%' blue→blue_shimmer.
    rec = {"state": "ACTIVE", "name": "p-001"}
    text = "⟳ 61% ok"
    out = statusline.render_segment(0.61, rec, True, now=0.62,
                                    shimmer=True)
    assert "\x1b[9" not in out
    assert "\x1b[7m" not in out
    assert "\x1b[38;2;185;230;180m1" in out  # '1' = green_shimmer
    assert "\x1b[38;2;180;205;240m%" in out  # '%' = blue_shimmer


def test_shimmer_band_is_brighter_same_hue_property():
    # the whole point of revision 9: at EVERY glimmer position, a band
    # char renders its OWN hue's shimmer variant (same hue, brighter
    # luminance — the contrast axis that actually survives the user's
    # display chain; the app's own input-box rainbow reads renkli),
    # non-band chars the base; no reverse, no 9x, no hue shift anywhere.
    import re as _re
    rec = {"state": "ACTIVE", "name": "p-001"}
    text = "⟳ 61% ok"
    n = len(text)

    def _lum(sgr):
        r, g, b = (int(x) for x in sgr[2:-1].split(";")[-3:])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    for g in range(n):
        # mid-tick (g+0.5): g * 0.05 lands exactly on a tick boundary,
        # where float division flips the floor — flaky glimmer
        out = statusline.render_segment(0.61, rec, True, now=(g + 0.5) * 0.05,
                                        shimmer=True)
        body = out[len(_BOLD):-len(_RESET)]
        groups = _re.findall(r"((?:\x1b\[[0-9;]*m)+.)", body, flags=_re.S)
        assert len(groups) == n, (g, out)
        for i, grp in enumerate(groups):
            idx = grp.rfind("\x1b")
            sgr = grp[idx:grp.rfind("m") + 1]  # escape right before the char
            assert _REVERSE not in grp, (g, i)     # no reverse video
            assert "\x1b[9" not in grp, (g, i)     # no 9x bright
            if abs(i - g) > 1:
                assert sgr == _RAINBOW[i % 7], (g, i)  # base
                continue
            assert sgr == _SHIMMER[i % 7], (g, i)  # same hue, brighter
            assert _lum(sgr) > _lum(_RAINBOW[i % 7]), (g, i)


def test_segment_ansi_rainbow_is_state_independent():
    # Revision 3: the user wants the ultrathink.sh rainbow, not per-state
    # hues. Every live state renders the same per-char rainbow; only the
    # segment text (and thus its length) varies by state.
    cases = [
        (0.63, {"name": "p-001", "state": "ROTATION_PENDING"},
         "⟳ 63% PENDING→p-002"),
        (0.7, {"name": "p-001", "state": "ROTATING"}, "⟳ 70% ROTATING"),
        (0.7, {"name": "p-001", "state": "AWAITING_MANUAL_CLEAR"},
         "⟳ 70% AMCL"),
        (0.73, {"name": "p-001", "state": "HANDOFF_REQUESTED"},
         "⟳ 73% HANDOFF!"),
    ]
    for ratio, rec, text in cases:
        assert statusline.render_segment(ratio, rec, True, now=0.0) == \
            _rainbow(text)


def test_segment_ansi_blocked_span_own_color():
    rec = {"state": "ACTIVE", "name": "p-001"}
    span = _BOLD + _R + " ⛔ BLOCKED" + _RESET
    assert statusline.render_segment(0.5, rec, True, blocked="x", now=0.0) \
        == _rainbow("⟳ 50% ok") + span
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


def test_run_ansi_on_renders_static_rainbow(cc_home, tmp_path, monkeypatch):
    # Phase 11 rev 4 default: no band (the shimmer needs "ultrathink" in
    # the prompt, rev 4 — see the block below).
    payload = _payload()
    monkeypatch.setattr(statusline.time, "time", lambda: 0.0)
    line = statusline.run(json.dumps(payload).encode(), _cfg())
    assert line == _rainbow("⟳ 9% ok")
    # wall clock moves, band stays off
    monkeypatch.setattr(statusline.time, "time", lambda: 0.55)
    line = statusline.run(json.dumps(payload).encode(), _cfg())
    assert line == _rainbow("⟳ 9% ok")


# -------------------------------- Phase 11 rev 4: reactive shimmer
#
# The bright band appears only while the pane's INPUT BOX (the last
# ❯-prefixed line through the next all-─ border line) contains the word
# "ultrathink" — scrollback never triggers. Fake tmux = test_rotator's
# argv-recording pattern, plus a canned pane file.

FAKE_TMUX_PANE = """#!/bin/sh
printf '%s\\n' "---" "$@" >> "${FAKE_TMUX_LOG:?}"
cat "${FAKE_PANE_FILE:?}"
"""

_BORDER = "─" * 80

# Keyword lives ONLY in scrollback — must never trigger (empty input box).
_PANE_DECOY = (
    "user: please run this with ultrathink\n"
    f"{_BORDER}\n"
    "❯ \n"
    f"{_BORDER}\n"
    "footer line\n"
)

_PANE_KEYWORD = (
    "scrollback line without the word\n"
    f"{_BORDER}\n"
    "❯ fix the ultrathink flag\n"
    f"{_BORDER}\n"
    "footer line\n"
)

# Wrapped prompt: the keyword sits on the continuation line, inside the box.
_PANE_WRAPPED = (
    f"{_BORDER}\n"
    "❯ a very long prompt that wraps onto the\n"
    "second line which carries ultrathink\n"
    f"{_BORDER}\n"
    "footer line\n"
)

_PANE_NO_BOX = (
    "ultrathink ultrathink\n"
    f"{_BORDER}\n"
    "footer line\n"
)


@pytest.fixture
def fake_tmux_pane(tmp_path, monkeypatch):
    """Fake tmux (``CC_CONTINUITY_TMUX_BIN``) that cats a canned pane file
    and records argv (test_rotator's ``---``-separated log format).
    Returns ``(pane_file, log)``; ``$TMUX`` is set."""
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(FAKE_TMUX_PANE)
    os.chmod(bin_path, 0o755)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(_PANE_KEYWORD, encoding="utf-8")
    log = tmp_path / "tmux-args.log"
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    monkeypatch.setenv("TMUX", "/tmp/fake,4242,0")
    return pane_file, log


def _set_pane(pane_file, text):
    pane_file.write_text(text, encoding="utf-8")


def _pane_calls(log) -> list:
    """Parse the fake-tmux log into argv lists (no calls: no log file)."""
    if not log.is_file():
        return []
    out, cur = [], None
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            if cur is not None:
                out.append(cur)
            cur = []
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        out.append(cur)
    return out


def test_prompt_has_ultrathink_in_input_box(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane  # default pane carries the keyword
    assert statusline.prompt_has_ultrathink() is True
    assert _pane_calls(log) == [["capture-pane", "-p"]]


def test_prompt_has_ultrathink_wrapped_input(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, _PANE_WRAPPED)
    assert statusline.prompt_has_ultrathink() is True


def test_prompt_has_ultrathink_case_insensitive(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, f"{_BORDER}\n❯ ULTRATHINK\n{_BORDER}\n")
    assert statusline.prompt_has_ultrathink() is True


def test_prompt_has_ultrathink_word_boundary(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, f"{_BORDER}\n❯ ultrathinker\n{_BORDER}\n")
    assert statusline.prompt_has_ultrathink() is False


def test_prompt_has_ultrathink_ignores_scrollback(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, _PANE_DECOY)
    assert statusline.prompt_has_ultrathink() is False


def test_prompt_has_ultrathink_no_input_box(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, _PANE_NO_BOX)
    assert statusline.prompt_has_ultrathink() is False


def test_prompt_has_ultrathink_without_tmux_env(cc_home, monkeypatch,
                                                fake_tmux_pane):
    pane, log = fake_tmux_pane
    monkeypatch.delenv("TMUX")
    assert statusline.prompt_has_ultrathink() is False
    assert _pane_calls(log) == []  # tmux never spawned


def test_prompt_has_ultrathink_tmux_failure(cc_home, tmp_path, monkeypatch,
                                            fake_tmux_pane):
    pane, log = fake_tmux_pane
    fail_bin = tmp_path / "fail-tmux"
    fail_bin.write_text("#!/bin/sh\nexit 3\n")
    os.chmod(fail_bin, 0o755)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(fail_bin))
    assert statusline.prompt_has_ultrathink() is False


def test_run_shimmers_when_prompt_has_ultrathink(cc_home, monkeypatch,
                                                 fake_tmux_pane):
    pane, log = fake_tmux_pane  # default pane carries the keyword
    monkeypatch.setattr(statusline.time, "time", lambda: 0.62)
    line = statusline.run(json.dumps(_payload()).encode(), _cfg())
    # revision 9 math: floor(0.62 / 0.05) % 7 = 5 → band = glimmer ± 1
    assert line == _rainbow("⟳ 9% ok", band={4, 5, 6})
    assert _pane_calls(log) == [["capture-pane", "-p"]]


def test_run_stays_static_when_prompt_clean(cc_home, monkeypatch,
                                            fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, _PANE_DECOY)
    monkeypatch.setattr(statusline.time, "time", lambda: 0.62)
    line = statusline.run(json.dumps(_payload()).encode(), _cfg())
    assert line == _rainbow("⟳ 9% ok")


def test_run_disabled_never_reads_pane(cc_home, fake_tmux_pane):
    # guard: a static/off segment must not even spawn tmux
    pane, log = fake_tmux_pane
    line = statusline.run(json.dumps(_payload()).encode(),
                          _cfg(enabled=False))
    assert line == "\x1b[90m⟳ off" + _RESET
    assert _pane_calls(log) == []


# --------------------------- render diagnostic log (flicker bug hunt)
#
# Every ``run()`` appends one line to ``runtime/statusline-render.log``:
# wall-clock ts, gate states (live/ansi/ut), the band's glimmer cell, and
# the ``❯`` line the gate looked at. The 2026-09-23 "shimmer not working"
# hunt needs exactly this evidence from LIVE renders (cadence + gate
# input) without guessing.


def _render_log_path(cc_home):
    return paths.runtime_dir() / "statusline-render.log"


def test_run_appends_render_log_with_gate_state(cc_home, monkeypatch,
                                                fake_tmux_pane):
    pane, log = fake_tmux_pane  # default pane carries the keyword
    monkeypatch.setattr(statusline.time, "time", lambda: 0.62)
    line = statusline.run(json.dumps(_payload()).encode(), _cfg())
    assert line == _rainbow("⟳ 9% ok", band={4, 5, 6})
    lp = _render_log_path(cc_home)
    assert lp.is_file()
    lines = lp.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    # now=0.62: floor(0.62/0.05) % 7 = 5 → glim=5
    assert "live=1" in lines[0]
    assert "ansi=1" in lines[0]
    assert "ut=1" in lines[0]
    assert "glim=5" in lines[0]
    assert "box='❯ fix the ultrathink flag'" in lines[0]


def test_run_render_log_static_when_box_clean(cc_home, monkeypatch,
                                              fake_tmux_pane):
    pane, log = fake_tmux_pane
    _set_pane(pane, _PANE_DECOY)  # keyword only in scrollback
    monkeypatch.setattr(statusline.time, "time", lambda: 0.62)
    statusline.run(json.dumps(_payload()).encode(), _cfg())
    line = _render_log_path(cc_home).read_text(encoding="utf-8").splitlines()[-1]
    assert "live=1" in line and "ut=0" in line and "glim=-" in line


def test_run_render_log_disabled_never_spawns(cc_home, fake_tmux_pane):
    pane, log = fake_tmux_pane
    statusline.run(json.dumps(_payload()).encode(), _cfg(enabled=False))
    line = _render_log_path(cc_home).read_text(encoding="utf-8").splitlines()[-1]
    assert "live=0" in line and "ut=0" in line
    assert _pane_calls(log) == []  # short-circuit preserved


def test_render_log_caps_growth(cc_home):
    lp = _render_log_path(cc_home)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("\n".join(f"old-{i:04d} padding-padding" for i in range(4000))
                  + "\n", encoding="utf-8")
    statusline.run(b"not json", _cfg(statusline_ansi=False))
    lines = lp.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 600  # capped: keep newest 500 + this render
    assert "live=" in lines[-1]


def test_render_log_never_breaks_run(cc_home):
    # a directory squatting on the log path must not break the status line
    lp = _render_log_path(cc_home)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.mkdir()
    line = statusline.run(json.dumps(_payload()).encode(),
                          _cfg(statusline_ansi=False))
    assert line == "⟳ 9% ok"
