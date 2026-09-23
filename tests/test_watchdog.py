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
