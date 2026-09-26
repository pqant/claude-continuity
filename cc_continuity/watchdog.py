"""Phase 10 pure decision core — LLM watchdog (spec P10-D4/D6/D7).

Zero I/O: health debounce, turn-liveness classification, nudge policy.
Everything that touches curl/tmux/ledger/store lives in ``watchdogd``.
The daemon feeds this core once per tick and acts on its answers (P10-D12).
"""

from __future__ import annotations

import dataclasses
import re

__all__ = [
    "ALIVE", "FROZEN", "IDLE_WAITING", "SKIP", "STALLED", "TURN_DEAD",
    "WAIT",
    "DONE", "GIVE_UP", "NUDGE", "RESEND", "SEND_ENTER", "WAIT_HEALTH",
    "ERROR_TAIL_RE", "PROMPT_RE",
    "Episode", "HealthState",
    "classify", "error_in_tail", "nudge_action", "pane_idle", "pane_pending",
    "power_cycle_due",
]

# --- liveness outcomes (P10-D6) ---------------------------------------------
ALIVE = "ALIVE"
IDLE_WAITING = "IDLE_WAITING"
TURN_DEAD = "TURN_DEAD"
STALLED = "STALLED"
FROZEN = "FROZEN"
SKIP = "SKIP"
WAIT = "WAIT"  # internal (P10-P2): row 5 needs 2 unchanged ticks

# --- nudge actions (P10-D7) ---------------------------------------------------
WAIT_HEALTH = "WAIT_HEALTH"
NUDGE = "NUDGE"
SEND_ENTER = "SEND_ENTER"
RESEND = "RESEND"
GIVE_UP = "GIVE_UP"
DONE = "DONE"

# --- pane markers (P10-D6; module constants, not config — YAGNI) -------------
#: Claude Code prompt glyph — "pane at prompt" = last non-empty line.
PROMPT_RE = re.compile(r"^\s*❯")
#: Error strings observed in CC 2.x failure panes (2026-09). The bare
#: ``error:`` option carries its own boundaries: a trailing ``\b`` inside
#: the group would never match after the colon (no word boundary follows
#: a non-word char) and silently drop every bare ``error: …`` line.
ERROR_TAIL_RE = re.compile(
    r"(?i)\b(api error|connection (closed|reset|refused)"
    r"|request timed out|retrying)\b|\berror\b:")


@dataclasses.dataclass
class HealthState:
    """Probe debounce (P10-D4): DOWN after 2 consecutive failures.

    ``feed(ok, now)`` returns the transition — ``"down"``, ``"up"`` — or
    None. Cold start is UNKNOWN; UNKNOWN→UP is silent (nothing was ever
    announced). ``down_since`` is set at the DOWN transition and cleared
    at the UP transition (read it BEFORE the feed that returns "up" —
    the daemon computes the downtime for the ``dgx_up`` body).
    """
    status: str = "UNKNOWN"  # UNKNOWN | UP | DOWN
    consecutive_fails: int = 0
    down_since: float | None = None
    fail_to_down: int = 2

    def feed(self, ok: bool, now: float) -> str | None:
        if ok:
            was_down = self.status == "DOWN"
            self.consecutive_fails = 0
            self.status = "UP"
            if was_down:
                self.down_since = None
                return "up"
            return None
        self.consecutive_fails += 1
        if self.status != "DOWN" and self.consecutive_fails >= self.fail_to_down:
            self.status = "DOWN"
            self.down_since = now
            return "down"
        return None


# --- pane inspection (P10-D6) -------------------------------------------------


def pane_idle(capture: str) -> bool:
    """True when the pane is AT the prompt (P10-D6 "pane at prompt"):
    the last non-empty line of the capture matches ``PROMPT_RE`` (the
    ``❯`` glyph). Trailing blank lines (capture padding) are skipped.
    """
    last = None
    for line in reversed(capture.splitlines()):
        if line.strip():
            last = line
            break
    return last is not None and bool(PROMPT_RE.match(last))


def error_in_tail(capture: str, n: int = 10) -> bool:
    """True when ``ERROR_TAIL_RE`` matches within the last ``n``
    NON-PROMPT lines of the capture (P10-D6: an hours-old scrollback
    error must not trigger; prompt lines are input, not error content).
    """
    non_prompt = [ln for ln in capture.splitlines()
                 if ln.strip() and not PROMPT_RE.match(ln)]
    return any(ERROR_TAIL_RE.search(ln) for ln in non_prompt[-n:])


def classify(rec: dict, last_ts: float | None, capture: str,
             prev_capture: str | None, now: float, stale_s: float,
             unchanged_ticks: int = 0) -> str:
    """P10-D6 liveness matrix (pure; row order per P10-P2).

    ``rec``: the state record (reads ``state``, ``turn_active``).
    ``last_ts``: ts of the LAST ledger entry of any type (P10-P9: any
    growth = activity; None = no ledger → stale). ``capture`` /
    ``prev_capture``: current / previous tick ``capture-pane -p``
    (prev None = first tick — no change evidence). ``unchanged_ticks``:
    consecutive ticks after the baseline that saw the SAME capture
    (P10-P3; FROZEN fires at >= 2). Returns one of the module outcome
    constants, or the internal ``WAIT`` (row 5 not yet armed).
    """
    if rec.get("state") not in ("ACTIVE", "WARNED"):
        return SKIP  # row 7, checked FIRST — eligibility dominates age
    if last_ts is not None and now - last_ts <= stale_s:
        return ALIVE  # row 1
    if prev_capture is not None and capture != prev_capture:
        return ALIVE  # row 2 — rendering / tool output
    if pane_idle(capture):
        if rec.get("turn_active"):
            return TURN_DEAD  # row 3 — mid-turn death, Stop never fired
        if error_in_tail(capture):
            return STALLED  # row 4 — turn ended in error
        return IDLE_WAITING  # row 6 — legitimately waiting for the user
    if unchanged_ticks >= 2:
        return FROZEN  # row 5 — input closed; detect + push, no nudge
    return WAIT  # internal: row 5 needs 2 unchanged ticks


def pane_pending(capture: str, nudge_text: str) -> bool:
    """True when the nudge text sits UNsubmitted in the input line: the
    last non-empty line is a prompt line containing ``nudge_text``
    (P10-D7.3 — the 11:30 lost-Enter case; the daemon then sends a bare
    Enter instead of re-typing the text).
    """
    last = None
    for line in reversed(capture.splitlines()):
        if line.strip():
            last = line
            break
    return (last is not None and bool(PROMPT_RE.match(last))
            and nudge_text in last)


# --- nudge state machine (P10-D7) --------------------------------------------


@dataclasses.dataclass
class Episode:
    """Per-(project, nnn) nudge episode (in memory; P10-D7/P10-P4).

    ``n``: attempts sent so far (NUDGE/RESEND count; SEND_ENTER does
    not — it completes the current attempt). ``nudge_ts``: ts of the
    last nudge (verification = a ``user_prompt`` ledger entry with
    ``ts > nudge_ts`` — the 11:30 lesson). ``waiting_health``:
    informational — the episode opened while the server was down.
    ``given_up``: GIVE_UP fired; DONE until a human ``user_prompt``
    resets the episode (the daemon performs the reset, P10-P4).
    """
    n: int = 0
    nudge_ts: float | None = None
    waiting_health: bool = False
    given_up: bool = False


def nudge_action(health: str, ep: Episode, pane_pending: bool,
                 max_nudges: int) -> str:
    """P10-D7 nudge state machine (pure — the daemon executes the
    answer and owns all side effects).

    ``health``: the current ``HealthState.status``. ``ep``: the
    episode so far. ``pane_pending``: the last nudge text sits
    unsubmitted in the input line (``pane_pending()``).
    ``max_nudges``: ``watchdog_max_nudges``.

    First nudge (``n == 0``): NUDGE only when health is UP — a nudge is
    a model call, so DOWN/UNKNOWN waits (P10-D7.1). Afterwards: pending
    input → SEND_ENTER (a bare Enter is free even while DOWN); text
    absent → RESEND (counts as the next attempt) until
    ``n >= max_nudges`` → GIVE_UP (P10-D7.4). ``given_up`` → DONE until
    a human ``user_prompt``.
    """
    if ep.given_up:
        return DONE
    if ep.n == 0:
        return NUDGE if health == "UP" else WAIT_HEALTH
    if pane_pending:
        return SEND_ENTER
    return RESEND if ep.n < max_nudges else GIVE_UP


# --- auto power-cycle (Phase 13, design 7899) ---------------------------------


def power_cycle_due(status: str, down_since: float | None, now: float,
                    last_cycle_ts: float | None,
                    after_s: float = 600.0,
                    cooldown_s: float = 1800.0) -> bool:
    """True when the daemon should power-cycle the DGX.

    Two independent gates (design 7899): the server has been DOWN for
    at least ``after_s`` (default 10 min — covers box boot + model
    load, so a slow boot is never cycled) AND the last successful
    cycle is at least ``cooldown_s`` old (default 30 min — no cycle
    loops). ``status`` / ``down_since`` are :class:`HealthState`
    fields; ``last_cycle_ts`` comes from runtime/power-cycle.json
    (None = never cycled). Pure — the daemon owns the curl and the
    file (P10-P1)."""
    if status != "DOWN" or down_since is None:
        return False
    if now - down_since < after_s:
        return False
    if last_cycle_ts is not None and now - last_cycle_ts < cooldown_s:
        return False
    return True
