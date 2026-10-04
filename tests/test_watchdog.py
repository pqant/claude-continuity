"""watchdog.py: Phase 10 pure decision core (spec P10-D4/D6/D7).

Zero I/O — every input is an argument, every output a value.
"""

import dataclasses

from cc_continuity import watchdog


# --- HealthState (P10-D4: 2-fail debounce, cold start UNKNOWN) --------------

def test_health_cold_start_first_failure_is_failure_1():
    h = watchdog.HealthState()
    assert h.status == "UNKNOWN"
    assert h.feed(ok=False, now=100.0) is None  # failure 1: no transition yet
    assert h.status == "UNKNOWN"
    assert h.consecutive_fails == 1


def test_health_down_after_two_consecutive_failures():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    assert h.feed(ok=False, now=400.0) == "down"
    assert h.status == "DOWN"
    assert h.down_since == 400.0


def test_health_one_fail_then_ok_stays_unknown():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    assert h.feed(ok=True, now=400.0) is None  # UNKNOWN → UP: never announced
    assert h.status == "UP"
    assert h.consecutive_fails == 0
    assert h.down_since is None


def test_health_up_to_down_transition():
    h = watchdog.HealthState()
    h.feed(ok=True, now=100.0)
    assert h.status == "UP"
    h.feed(ok=False, now=400.0)
    assert h.feed(ok=False, now=700.0) == "down"
    assert h.down_since == 700.0


def test_health_down_to_up_transition():
    h = watchdog.HealthState()
    h.feed(ok=False, now=100.0)
    h.feed(ok=False, now=400.0)          # DOWN
    assert h.feed(ok=True, now=2000.0) == "up"
    assert h.status == "UP"
    assert h.down_since is None  # consumed by the daemon for the push body


# --- pane inspection (P10-D6 "pane at prompt" / "error in tail") ------------

NOW = 10_000.0
STALE_S = 300.0
FRESH_TS = NOW - 10.0
STALE_TS = NOW - 301.0  # 301 s of silence > 300 s threshold → stale
CAP_PROMPT = "ran tool: Read\n❯ "
CAP_NOT_PROMPT = "▓▓▓ 42% thinking"


def test_pane_idle_last_nonempty_line_is_prompt():
    assert watchdog.pane_idle("output\n❯ ") is True
    assert watchdog.pane_idle("output\n  ❯ ") is True  # PROMPT_RE: ^\s*❯


def test_pane_idle_ignores_trailing_blanks():
    assert watchdog.pane_idle("output\n❯ \n\n   \n") is True


def test_pane_idle_not_at_prompt():
    assert watchdog.pane_idle("output\nspinner 42%") is False


def test_pane_idle_empty_capture():
    assert watchdog.pane_idle("") is False
    assert watchdog.pane_idle("\n   \n") is False


def test_error_in_tail_matches_observed_cc_strings():
    for s in ("API Error: 503", "error: connection refused",
              "Connection closed by peer", "Request timed out",
              "Retrying in 5s..."):
        assert watchdog.error_in_tail(f"{s}\n❯ ") is True, s


def test_error_in_tail_window_is_last_ten_nonprompt_lines():
    inside = "\n".join([f"line {i}" for i in range(10)] + ["error: boom"])
    assert watchdog.error_in_tail(inside + "\n❯ ") is True
    outside = "\n".join(["error: boom"] + [f"line {i}" for i in range(10)])
    assert watchdog.error_in_tail(outside + "\n❯ ") is False


def test_error_in_tail_excludes_prompt_lines():
    # a prompt containing the word "error:" is input, not an error marker
    cap = "\n".join([f"line {i}" for i in range(10)]
                    + ["❯ fix the error: in auth"])
    assert watchdog.error_in_tail(cap) is False
    assert watchdog.pane_idle(cap) is True  # the prompt is the last line


# --- classify (full P10-D6 matrix, row order P10-P2) -------------------------


def test_classify_skip_dominates_fresh_ledger():
    # row 7 checked FIRST: mid-rotation state, even a fresh ledger → SKIP
    rec = {"state": "ROTATION_PENDING", "turn_active": True}
    assert watchdog.classify(rec, FRESH_TS, CAP_PROMPT, None,
                             NOW, STALE_S) == "SKIP"


def test_classify_skip_non_eligible_states():
    for state in ("BOOTSTRAPPING", "AWAITING_MANUAL_CLEAR",
                  "HANDOFF_REQUESTED", "ROTATING", "ROTATED"):
        rec = {"state": state, "turn_active": True}
        assert watchdog.classify(rec, STALE_TS, CAP_PROMPT, CAP_PROMPT,
                                 NOW, STALE_S) == "SKIP"


def test_classify_alive_on_fresh_ledger():
    rec = {"state": "ACTIVE", "turn_active": True}
    assert watchdog.classify(rec, FRESH_TS, CAP_NOT_PROMPT, CAP_NOT_PROMPT,
                             NOW, STALE_S) == "ALIVE"


def test_classify_alive_when_pane_changed():
    # row 2: stale ledger, but the pane rendered something new this tick
    rec = {"state": "ACTIVE", "turn_active": True}
    assert watchdog.classify(rec, STALE_TS, "fresh output line",
                             "older output line", NOW, STALE_S) == "ALIVE"


def test_classify_turn_dead_mid_turn_death():
    # row 3: stale + at prompt + unchanged + turn never stopped
    rec = {"state": "ACTIVE", "turn_active": True}
    assert watchdog.classify(rec, STALE_TS, CAP_PROMPT, CAP_PROMPT,
                             NOW, STALE_S) == "TURN_DEAD"


def test_classify_stalled_error_in_tail():
    # row 4: turn stopped (turn_active False) but the turn ended in error
    rec = {"state": "ACTIVE", "turn_active": False}
    cap = "API Error: connection reset by peer\n❯ "
    assert watchdog.classify(rec, STALE_TS, cap, cap, NOW, STALE_S) \
        == "STALLED"


def test_classify_idle_waiting_no_error():
    # row 6: legitimately waiting for the user
    rec = {"state": "WARNED", "turn_active": False}
    assert watchdog.classify(rec, STALE_TS, CAP_PROMPT, CAP_PROMPT,
                             NOW, STALE_S) == "IDLE_WAITING"


def test_classify_old_error_outside_tail_window_is_idle():
    # an hours-old error must not trigger STALLED (10 non-prompt lines)
    rec = {"state": "ACTIVE", "turn_active": False}
    lines = ["old error: boom"] + [f"line {i}" for i in range(10)]
    cap = "\n".join(lines) + "\n❯ "
    assert watchdog.classify(rec, STALE_TS, cap, cap, NOW, STALE_S) \
        == "IDLE_WAITING"


def test_classify_frozen_at_two_unchanged_ticks():
    # row 5: not at prompt, capture unchanged for 2 consecutive ticks
    rec = {"state": "ACTIVE", "turn_active": False}
    assert watchdog.classify(rec, STALE_TS, CAP_NOT_PROMPT, CAP_NOT_PROMPT,
                             NOW, STALE_S, unchanged_ticks=2) == "FROZEN"


def test_classify_wait_before_two_ticks():
    # P10-P3: the first comparison tick must do nothing
    rec = {"state": "ACTIVE", "turn_active": False}
    for ticks in (0, 1):
        assert watchdog.classify(rec, STALE_TS, CAP_NOT_PROMPT,
                                 CAP_NOT_PROMPT, NOW, STALE_S,
                                 unchanged_ticks=ticks) == "WAIT"


def test_classify_first_tick_no_prev_capture():
    # prev None: row 2 can't fire; at-prompt rows still decide (P10-P2)
    rec = {"state": "ACTIVE", "turn_active": True}
    assert watchdog.classify(rec, STALE_TS, CAP_PROMPT, None,
                             NOW, STALE_S) == "TURN_DEAD"


def test_classify_no_ledger_treated_as_stale():
    rec = {"state": "ACTIVE", "turn_active": False}
    assert watchdog.classify(rec, None, CAP_PROMPT, CAP_PROMPT,
                             NOW, STALE_S) == "IDLE_WAITING"


# --- pane_pending (P10-D7.3: nudge text sitting unsubmitted) ----------------


def test_pane_pending_nudge_sitting_in_input_line():
    assert watchdog.pane_pending("output\n❯ continue", "continue") is True


def test_pane_pending_empty_prompt():
    assert watchdog.pane_pending("output\n❯ ", "continue") is False


def test_pane_pending_not_at_prompt():
    assert watchdog.pane_pending("continue was typed\nspinner",
                                 "continue") is False


def test_pane_pending_submitted_text_is_not_pending():
    cap = "❯ continue\noutput\n❯ "
    assert watchdog.pane_pending(cap, "continue") is False


def test_pane_pending_multiline_nudge_in_input_box():
    # Phase 19: the nudge is MULTI-LINE — the ❯ glyph sits on the FIRST
    # input line, the capture's last line is the nudge's own tail line.
    cap = ("output\n❯ continue\n"
           "[cc-continuity watchdog] The model server is back online:\n"
           "DOWN 02:46:40 → UP 02:55:00 (15 min down)\n"
           "The in-flight turn died with the model. Continue from where you left off.\n"
           "If unsure, check git status and the last output first.")
    text = ("continue\n\n[cc-continuity watchdog] The model server is back online:\n"
            "DOWN 02:46:40 → UP 02:55:00 (15 min down)\n"
            "The in-flight turn died with the model. Continue from where you left off.\n"
            "If unsure, check git status and the last output first.")
    assert watchdog.pane_pending(cap, text) is True


def test_pane_pending_multiline_tail_mismatch_is_false():
    # the pane's tail line is NOT the sent nudge's tail -> not pending
    cap = "output\n❯ continue\nsome other tail line"
    text = ("continue\n\n[cc-continuity watchdog] "
            "The turn stalled (50 min idle) — Continue from where you left off.")
    assert watchdog.pane_pending(cap, text) is False


# --- pane_pending Phase 29: input-box aware ----------------------------------
# The statusline block renders BELOW the box's bottom frame line, so the
# capture's last line is a status line, never the box: a pending nudge is
# invisible to the legacy last-line match. The box's last line sits directly
# ABOVE the bottom-most frame line.


_FRAME = "─" * 100
_STATUS = ("  [qwen3.8-27b | API] │ proj git:(main) │ sess-001\n"
           "  Context ████░░░░░░ 39%\n"
           "  ⏵⏵ bypass permissions on (shift+tab to cycle)")


def test_pane_pending_box_above_statusline_block():
    # the multi-line nudge sits in the box (❯ on the first line, tail line
    # directly above the bottom frame); the statusline block is below.
    tail = "[cc-continuity watchdog] The turn stalled (50 min idle) — Continue from where you left off."
    cap = (f"⏺ done\n❯ continue\n{_FRAME}\n{tail}\n{_FRAME}\n{_STATUS}\n")
    text = "continue\n\n" + tail
    assert watchdog.pane_pending(cap, text) is True


def test_pane_pending_wrapped_tail_line():
    # the nudge's last line WRAPS at the pane width: the line above the
    # bottom frame is the tail fragment (no ❯ glyph).
    cap = (f"⏺ done\n❯ continue\n{_FRAME}\n"
           "If unsure, check git status and\n"
           "the last output.\n"
           f"{_FRAME}\n{_STATUS}\n")
    text = ("continue\n\n"
            "If unsure, check git status and the last output first.")
    assert watchdog.pane_pending(cap, text) is True


def test_pane_pending_empty_box_with_statusline():
    cap = f"⏺ done\n❯\n{_FRAME}\n{_STATUS}\n"
    assert watchdog.pane_pending(cap, "continue") is False


def test_pane_pending_other_text_in_box_is_false():
    cap = f"⏺ done\n❯ something else\n{_FRAME}\n{_STATUS}\n"
    assert watchdog.pane_pending(cap, "continue") is False


# --- build_nudge_text (Phase 19: the bare "continue" is "too little") ---------


def test_build_nudge_text_outage_block():
    text = watchdog.build_nudge_text("continue", now=10_900.0,
                                     down_since=10_000.0,
                                     cycle_ts=10_500.0)
    lines = text.splitlines()
    assert lines[0] == "continue"
    assert lines[1] == ""
    assert lines[2] == "[cc-continuity watchdog] The model server is back online:"
    assert lines[3].startswith("DOWN ")
    assert " → power_cycle " in lines[3]
    assert " → UP " in lines[3]
    assert "(15 min down)" in lines[3]
    assert lines[4] == ("The in-flight turn died with the model. "
                        "Continue from where you left off.")
    assert lines[5] == "If unsure, check git status and the last output first."
    assert len(lines) == 6


def test_build_nudge_text_outage_without_cycle():
    text = watchdog.build_nudge_text("continue", now=10_600.0,
                                     down_since=10_000.0)
    assert "power_cycle" not in text
    assert "(10 min down)" in text


def test_build_nudge_text_plain_turn_dead():
    text = watchdog.build_nudge_text("continue", now=10_000.0,
                                     idle_minutes=50)
    assert text == ("continue\n\n[cc-continuity watchdog] "
                    "The turn stalled (50 min idle) "
                    "— Continue from where you left off.")


def test_build_nudge_text_stalled():
    text = watchdog.build_nudge_text("continue", now=10_000.0,
                                     idle_minutes=50, stalled=True)
    assert text == ("continue\n\n[cc-continuity watchdog] "
                    "The last turn ended in error — continue from where you left off.")


def test_build_nudge_text_frozen():
    # Phase 22: the frozen-turn escalation — the daemon ESC-cancelled the
    # in-flight turn, so the text says so and tells the session to check
    # its state before resuming
    text = watchdog.build_nudge_text("continue", now=10_000.0,
                                     frozen_minutes=25)
    assert text == ("continue\n\n[cc-continuity watchdog] "
                    "Turn 25 min frozen — I cancelled it. "
                    "check git status and the last output, "
                    "Continue from where you left off.")


def test_build_nudge_text_outage_wins_over_frozen():
    # the OUTAGE form is the more specific context (the model went down
    # and came back) — it must not be replaced by the frozen block
    text = watchdog.build_nudge_text("continue", now=10_900.0,
                                     down_since=10_000.0,
                                     frozen_minutes=25)
    assert "dondu" not in text
    assert "The model server is back online" in text


# --- frozen_nudge_due (Phase 22: frozen-turn escalation threshold) ------------


def test_frozen_nudge_due_requires_turn_active():
    # a frozen pane WITHOUT turn_active is a picker / a legitimate user
    # wait — never escalated (the picker only gets a single Esc, Phase C)
    assert watchdog.frozen_nudge_due(10, False, frozen_minutes=20,
                                     interval_s=300) is False


def test_frozen_nudge_due_at_threshold():
    # 4 unchanged ticks x 300s = 20 min = the default threshold
    assert watchdog.frozen_nudge_due(4, True, frozen_minutes=20,
                                     interval_s=300) is True


def test_frozen_nudge_due_below_threshold():
    assert watchdog.frozen_nudge_due(3, True, frozen_minutes=20,
                                     interval_s=300) is False


def test_frozen_nudge_due_custom_minutes():
    # 10 min at 300s ticks = 2 unchanged ticks
    assert watchdog.frozen_nudge_due(2, True, frozen_minutes=10,
                                     interval_s=300) is True
    assert watchdog.frozen_nudge_due(1, True, frozen_minutes=10,
                                     interval_s=300) is False


def test_frozen_nudge_due_zero_minutes_due_immediately():
    # 0 = escalate as soon as the caller reaches the FROZEN branch
    # (only possible at ticks >= 2)
    assert watchdog.frozen_nudge_due(2, True, frozen_minutes=0,
                                     interval_s=300) is True


def test_frozen_nudge_due_bad_interval_is_false():
    # a non-positive interval would break the math — never due
    assert watchdog.frozen_nudge_due(4, True, frozen_minutes=20,
                                     interval_s=0) is False
    assert watchdog.frozen_nudge_due(4, True, frozen_minutes=20,
                                     interval_s=-5) is False


# --- Episode + nudge_action (P10-D7 table) ----------------------------------


def test_episode_defaults():
    ep = watchdog.Episode()
    assert ep.n == 0
    assert ep.nudge_ts is None
    assert ep.waiting_health is False
    assert ep.given_up is False


def test_nudge_action_first_up_nudges():
    assert watchdog.nudge_action("UP", watchdog.Episode(), False, 3) \
        == "NUDGE"


def test_nudge_action_first_down_or_unknown_waits():
    for health in ("DOWN", "UNKNOWN"):
        assert watchdog.nudge_action(health, watchdog.Episode(), False, 3) \
            == "WAIT_HEALTH"


def test_nudge_action_pending_input_send_enter_even_when_down():
    # Enter is free (no model call) — submit the waiting nudge regardless
    ep = watchdog.Episode(n=1, nudge_ts=1.0)
    for health in ("UP", "DOWN"):
        assert watchdog.nudge_action(health, ep, True, 3) == "SEND_ENTER"


def test_nudge_action_fresh_episode_pending_submits():
    # Phase 19 (daemon restart): the episode is GONE (in-memory) but a
    # nudge sits unsubmitted in the input box — a fresh episode with
    # pending input must Enter-submit it, NOT re-paste (re-pasting would
    # duplicate the nudge in the box).
    ep = watchdog.Episode()
    assert ep.n == 0
    assert watchdog.nudge_action("UP", ep, True, 3) == "SEND_ENTER"


def test_nudge_action_unverified_resend_counts_as_next_attempt():
    for n in (1, 2):
        ep = watchdog.Episode(n=n, nudge_ts=float(n))
        assert watchdog.nudge_action("UP", ep, False, 3) == "RESEND"


def test_nudge_action_give_up_after_max_unverified():
    ep = watchdog.Episode(n=3, nudge_ts=3.0)
    assert watchdog.nudge_action("UP", ep, False, 3) == "GIVE_UP"


def test_nudge_action_given_up_done_until_human():
    ep = watchdog.Episode(n=3, nudge_ts=3.0, given_up=True)
    assert watchdog.nudge_action("UP", ep, False, 3) == "DONE"
    assert watchdog.nudge_action("DOWN", ep, True, 3) == "DONE"


def test_nudge_action_max_one_nudge():
    assert watchdog.nudge_action("UP", watchdog.Episode(), False, 1) \
        == "NUDGE"
    ep = watchdog.Episode(n=1, nudge_ts=1.0)
    assert watchdog.nudge_action("UP", ep, False, 1) == "GIVE_UP"


# --- power cycle (Phase 13 design 7899: sustained DOWN -> relay cycle) ------

def test_power_cycle_not_due_when_up():
    assert watchdog.power_cycle_due("UP", None, 10_000.0, None) is False


def test_power_cycle_not_due_when_down_briefly():
    # DOWN for 5 min < 10 min threshold (default after_s=600)
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 300.0,
                                    10_000.0, None) is False


def test_power_cycle_due_at_threshold():
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 600.0,
                                    10_000.0, None) is True


def test_power_cycle_not_due_with_missing_down_since():
    assert watchdog.power_cycle_due("DOWN", None, 10_000.0, None) is False


def test_power_cycle_blocked_by_active_cooldown():
    # 20 min since last cycle < 30 min cooldown (default cooldown_s=1800)
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 2000.0, 10_000.0,
                                    10_000.0 - 1200.0) is False


def test_power_cycle_due_after_cooldown_expired():
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 2000.0, 10_000.0,
                                    10_000.0 - 1800.0) is True


def test_power_cycle_custom_thresholds():
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 100.0, 10_000.0,
                                    None, after_s=60.0,
                                    cooldown_s=30.0) is True
    assert watchdog.power_cycle_due("DOWN", 10_000.0 - 30.0, 10_000.0,
                                    None, after_s=60.0,
                                    cooldown_s=30.0) is False


# --- Phase 21: exit-hang killer (cp-059 design — /exit marker forensics) -----
#
# The real /exit marker (verified across 109 transcripts): a ``user`` entry
# whose RAW STRING content STARTS WITH the command-name tag. Naive substring
# scans false-positive on investigation transcripts that QUOTE the marker in
# assistant text / embedded user text. After a HUNG marker the only entries
# written are ceremony entries (farewell stdout, caveat, "No response
# requested."); ANY other entry after the marker = the process is alive →
# no kill. entries = list[(ts: float, etype: str, text: str)].


def _e(ts, etype, text=""):
    return (ts, etype, text)


T0 = 1_000_000.0
MARK = "<command-name>/exit</command-name>"
MARK_FULL = (MARK + "             <command-message>exit</command-message>"
             "             <command-args></command-args>")
FARWELL = "<local-command-stdout>See ya!</local-command-stdout>"
CAVEAT = "<local-command-caveat>Caveat: local command output follows.</local-command-caveat>"


def test_exit_marker_is_the_exact_command_name_tag():
    # the forensic rule is startswith on this exact tag — a "simplification"
    # to a bare substring is a false positive (058/059 transcripts proved it)
    assert watchdog.EXIT_MARKER == MARK


# --- parse_entry_ts -----------------------------------------------------------

def test_parse_entry_ts_zulu_iso_to_epoch():
    from datetime import datetime, timezone
    want = datetime(2026, 9, 28, 20, 43, 17, 123456,
                    tzinfo=timezone.utc).timestamp()
    assert watchdog.parse_entry_ts("2026-09-28T20:43:17.123456Z") == want


def test_parse_entry_ts_malformed_returns_none():
    # a NAIVE ts is malformed too: interpreting it as local time would
    # silently produce a WRONG epoch, so reject it
    for bad in ("", "not-a-date", "Z", "2026-13-99T99:99:99Z",
                "2026-09-28T20:43:17", "2026-09-28 20:43:17"):
        assert watchdog.parse_entry_ts(bad) is None, repr(bad)


# --- transcript_slug ----------------------------------------------------------

def test_transcript_slug_matches_cc_project_dirs():
    # verified against real ~/.claude/projects/ dir names (cp-059 item 4):
    # every non-alphanumeric character becomes '-' — the dot in a hidden
    # dir yields a DOUBLE dash
    assert watchdog.transcript_slug(
        "/Users/devx/arid/claude-continuity") \
        == "-Users-devx-arid-claude-continuity"
    assert watchdog.transcript_slug("/Users/devx/.claude") \
        == "-Users-devx--claude"


# --- find_exit_marker_ts ------------------------------------------------------

def test_find_exit_marker_ts_found_on_user_raw_string():
    entries = [_e(T0 - 60, "assistant", "bye soon"),
               _e(T0, "user", MARK_FULL),
               _e(T0 + 7, "assistant", "No response requested.")]
    assert watchdog.find_exit_marker_ts(entries) == T0


def test_find_exit_marker_ts_none_when_absent():
    entries = [_e(T0, "user", "normal prompt"),
               _e(T0 + 1, "assistant", "ok")]
    assert watchdog.find_exit_marker_ts(entries) is None


def test_find_exit_marker_ts_ignores_quoted_marker():
    # investigation transcripts QUOTE the marker in assistant text and in
    # user text that merely contains it — the rule is user-type ONLY and
    # startswith, never substring
    entries = [_e(T0, "assistant", f"the marker is {MARK_FULL}"),
               _e(T0 + 1, "user", "test: " + MARK_FULL + " again")]
    assert watchdog.find_exit_marker_ts(entries) is None


def test_find_exit_marker_ts_max_of_retries():
    entries = [_e(T0, "user", MARK_FULL),
               _e(T0 + 50, "user", MARK_FULL)]
    assert watchdog.find_exit_marker_ts(entries) == T0 + 50


# --- exit_hang_due --------------------------------------------------------------

def test_exit_hang_due_false_without_marker():
    entries = [_e(T0, "user", "hi"), _e(T0 + 1, "assistant", "hello")]
    assert watchdog.exit_hang_due(entries, T0 + 3600, 30.0) is False


def test_exit_hang_due_boundary_at_limit_false_strict_after():
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 30, 30.0) is False
    assert watchdog.exit_hang_due(entries, T0 + 30.5, 30.0) is True


def test_exit_hang_due_false_when_process_not_alive():
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 3600, 30.0,
                                  alive=False) is False


def test_exit_hang_due_true_stale_marker_nothing_after():
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0) is True


def test_exit_hang_due_true_when_only_ceremony_after_marker():
    # every hung case ever observed wrote ONLY these after the marker
    entries = [
        _e(T0, "user", MARK_FULL),
        _e(T0, "user", FARWELL),          # same ts as the marker
        _e(T0, "user", CAVEAT),           # same ts as the marker
        _e(T0 + 7, "assistant", "No response requested."),
    ]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0) is True


def test_exit_hang_due_synthetic_text_strip_tolerant():
    entries = [_e(T0, "user", MARK_FULL),
               _e(T0 + 7, "assistant", "  No response requested.  ")]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0) is True


def test_exit_hang_due_false_when_real_activity_after_marker():
    # each of these after a stale marker means the process IS alive
    real = [
        _e(T0 + 2, "user", "yeni prompt atildi"),
        _e(T0 + 2, "assistant", "an actual answer"),
        _e(T0 + 2, "assistant", ""),               # tool-only turn
        _e(T0 + 2, "attachment", ""),
        _e(T0 + 2, "system", "some system line"),
        _e(T0 + 2, "file-history-delta", ""),
    ]
    for after in real:
        entries = [_e(T0, "user", MARK_FULL), after]
        assert watchdog.exit_hang_due(entries, T0 + 3600, 30.0) is False, after


def test_exit_hang_due_age_measured_from_newest_marker():
    # the user retried /exit: the fresh marker resets the clock
    entries = [_e(T0, "user", MARK_FULL),
               _e(T0 + 10, "user", MARK_FULL)]
    # age from the NEWEST marker (T0+10): 30 s at now=T0+40 is not > 30,
    # 35 s at now=T0+45 is — measuring from the oldest marker would make
    # the first assertion True (40 > 30), so it pins the newest rule
    assert watchdog.exit_hang_due(entries, T0 + 40, 30.0) is False
    assert watchdog.exit_hang_due(entries, T0 + 45, 30.0) is True


def test_exit_hang_due_activity_before_marker_ignored():
    entries = [_e(T0 - 50, "user", "old prompt"),
               _e(T0 - 40, "assistant", "old answer"),
               _e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0) is True


# --- cp-069: the marker must be this process's own ------------------------------
#
# read_transcript_entries picks the NEWEST transcript in the project dir by
# mtime. That file may belong to an OLDER conversation: a --continue/--resume
# replay re-serves an old tail, or a fresh process is read before its own
# transcript exists. In both cases the /exit marker predates the process.
# The discriminator: a marker older than the process START (minus a small
# epsilon for ps second-granularity) is not this process's /exit -> no kill.

def test_exit_hang_due_false_when_marker_older_than_process_start():
    # a 2-day-old marker, a process that started an hour ago: not this
    # process's /exit -> never due
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 172800, 30.0,
                                  start_ts=T0 + 3600) is False


def test_exit_hang_due_true_when_marker_newer_than_process_start():
    # the REAL hang (20.8h, 2026-09-29): the marker was written by the
    # process ITSELF, long after its start -> still due
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0,
                                  start_ts=T0 - 3600) is True


def test_exit_hang_due_start_ts_epsilon_boundary():
    # ps etime is second-granular: a marker a FEW seconds before the
    # (rounded) start estimate is not yet "older" — the epsilon spares it
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0,
                                  start_ts=T0 + 3) is True
    assert watchdog.exit_hang_due(entries, T0 + 3600, 30.0,
                                  start_ts=T0 + 10) is False


def test_exit_hang_due_no_start_ts_keeps_old_behavior():
    # start_ts=None (no start-time info) -> the guard is INACTIVE: the
    # pre-cp-069 decision stands (grace + activity only)
    entries = [_e(T0, "user", MARK_FULL)]
    assert watchdog.exit_hang_due(entries, T0 + 31, 30.0,
                                  start_ts=None) is True


# --- find_claude_pid -------------------------------------------------------------

def test_find_claude_pid_root_itself_is_claude():
    # sh exec optimization: pane_pid itself IS the claude process
    procmap = {100: (99, "claude --dangerously-skip-permissions -n x")}
    assert watchdog.find_claude_pid(100, procmap) == 100


def test_find_claude_pid_child_of_sh_wrapper():
    # the live map (cp-059): pane_pid = sh wrapper, claude is its CHILD
    procmap = {24817: (99, "/bin/sh /usr/bin/command claude -n old"),
               24822: (24817, "claude --dangerously-skip-permissions -n old")}
    assert watchdog.find_claude_pid(24817, procmap) == 24822


def test_find_claude_pid_path_form_command():
    procmap = {10: (1, "/opt/homebrew/bin/claude -p hi"),
               11: (10, "sh -c sleep")}
    assert watchdog.find_claude_pid(10, procmap) == 10


def test_find_claude_pid_none_when_absent_or_root_missing():
    procmap = {10: (1, "zsh"),
               11: (10, "cc-continuity progress-pane @23"),
               12: (10, "sleep 5")}
    assert watchdog.find_claude_pid(10, procmap) is None
    assert watchdog.find_claude_pid(999, procmap) is None


def test_find_claude_pid_empty_command_is_not_claude():
    # a zombie row may carry an empty command string
    procmap = {1: (0, "zsh"), 2: (1, "")}
    assert watchdog.find_claude_pid(1, procmap) is None


def test_find_claude_pid_shallowest_match_wins():
    # BFS from the root: the TOPMOST claude in the tree is the target
    procmap = {1: (0, "tmux: server"),
               2: (1, "sh -c wrapper"),
               3: (2, "claude -n a"),          # depth 2
               4: (1, "claude -n b")}          # depth 1
    assert watchdog.find_claude_pid(1, procmap) == 4


def test_find_claude_pid_exact_basename_only():
    # "claude-hud" / cc-continuity tooling is NOT the claude binary
    procmap = {1: (0, "zsh"),
               2: (1, "claude-hud --statusline"),
               3: (1, "python3 -m cc_continuity progress-pane")}
    assert watchdog.find_claude_pid(1, procmap) is None
