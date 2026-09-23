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
#: Error strings observed in CC 2.x failure panes (2026-09).
ERROR_TAIL_RE = re.compile(
    r"(?i)\b(api error|error:|connection (closed|reset|refused)"
    r"|request timed out|retrying)\b")


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
