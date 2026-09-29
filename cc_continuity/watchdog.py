"""Phase 10 pure decision core — LLM watchdog (spec P10-D4/D6/D7).

Zero I/O: health debounce, turn-liveness classification, nudge policy.
Everything that touches curl/tmux/ledger/store lives in ``watchdogd``.
The daemon feeds this core once per tick and acts on its answers (P10-D12).
"""

from __future__ import annotations

import dataclasses
import os
import re
from datetime import datetime

__all__ = [
    "ALIVE", "FROZEN", "IDLE_WAITING", "SKIP", "STALLED", "TURN_DEAD",
    "WAIT",
    "DONE", "GIVE_UP", "NUDGE", "RESEND", "SEND_ENTER", "WAIT_HEALTH",
    "ERROR_TAIL_RE", "PROMPT_RE",
    "Episode", "HealthState",
    "build_nudge_text", "classify", "error_in_tail", "nudge_action",
    "pane_idle", "pane_pending", "power_cycle_due",
    "EXIT_MARKER", "EXIT_CEREMONY_PREFIXES", "EXIT_SYNTHETIC",
    "parse_entry_ts", "transcript_slug", "find_exit_marker_ts",
    "exit_hang_due", "find_claude_pid",
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


def frozen_nudge_due(ticks: int, turn_active: bool, *,
                     frozen_minutes: float = 20.0,
                     interval_s: float = 300.0) -> bool:
    """Phase 22: has the FROZEN pane been frozen long enough to escalate?

    The freeze duration is ``ticks * interval_s`` (consecutive
    unchanged full ticks; the caller only reaches this in the FROZEN
    branch, i.e. ticks >= 2). ``turn_active`` is the "work is
    in-flight" signal — a frozen pane WITHOUT it (a picker, a
    legitimate user wait) is never escalated here. ``frozen_minutes``
    is ``watchdog_frozen_nudge_minutes`` (default 20); at 300 s ticks
    the default needs ticks >= 4. A non-positive ``interval_s`` is
    never due (broken config must not escalate)."""
    if not turn_active or interval_s <= 0:
        return False
    minutes = max(0.0, frozen_minutes)
    return ticks * interval_s >= minutes * 60


def pane_pending(capture: str, nudge_text: str) -> bool:
    """True when the nudge text sits UNsubmitted in the input line: the
    last non-empty line is a prompt line containing ``nudge_text``
    (P10-D7.3 — the 11:30 lost-Enter case; the daemon then sends a bare
    Enter instead of re-typing the text).

    Phase 19 — multiline: a ``build_nudge_text`` nudge sits in the input
    box with the ``❯`` glyph on the FIRST input line only; the capture's
    last non-empty line is the nudge's own TAIL line (a long line may also
    wrap, but the tail-match below covers the common case). So ALSO: the
    nudge's last non-empty line (stripped) appears in the capture's last
    non-empty line (stripped).
    """
    last = None
    for line in reversed(capture.splitlines()):
        if line.strip():
            last = line
            break
    if (last is not None and bool(PROMPT_RE.match(last))
            and nudge_text in last):
        return True
    nudge_last = None
    for line in reversed(nudge_text.splitlines()):
        if line.strip():
            nudge_last = line.strip()
            break
    return (nudge_last is not None and last is not None
            and nudge_last in last.strip())


# --- nudge text (Phase 19: the bare "continue" is "too little") ----------------


def _ts_hms(ts: float) -> str:
    """Local ``HH:MM:SS`` for a ts (same local-time basis as the
    watchdog.log lines the user reads)."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def build_nudge_text(base: str, *, now: float,
                     down_since: float | None = None,
                     cycle_ts: float | None = None,
                     idle_minutes: int | None = None,
                     stalled: bool = False,
                     frozen_minutes: int | None = None) -> str:
    """Phase 19: the nudge prompt carries WHY the turn stopped, so the
    session resumes with context instead of a bare ``continue``.

    ``base``: the first line — ``watchdog_nudge_text`` (the user's
    override wins). Forms (first match wins):

    * OUTAGE (``down_since`` set): the model server went down and came
      back; the in-flight turn died with the model. The text names the
      window (``DOWN <ts> → UP <ts> (N min down)``) — with a
      ``→ power_cycle <ts>`` segment ONLY when ``cycle_ts`` falls INSIDE
      that window — and tells the session to check ``git status`` / the
      last output before continuing.
    * FROZEN (Phase 22, ``frozen_minutes`` set): the pane was frozen
      with the turn in flight; the daemon ESC-cancelled the in-flight
      turn first — the text says so and tells the session to check
      ``git status`` / the last output before resuming.
    * PLAIN (``down_since`` None): TURN_DEAD → the idle minutes;
      STALLED (``stalled=True``) → the turn ended in error.

    The context block is prefixed ``[cc-continuity watchdog]`` so the
    session (and the user, scrolling) can see it is automation.
    """
    lines = [str(base), ""]
    if down_since is not None:
        if cycle_ts is not None and down_since <= cycle_ts <= now:
            cycle_seg = f" → power_cycle {_ts_hms(cycle_ts)}"
        else:
            cycle_seg = ""
        mins = int((now - down_since) // 60)
        lines += [
            "[cc-continuity watchdog] The model server is back online:",
            f"DOWN {_ts_hms(down_since)}{cycle_seg} → UP {_ts_hms(now)}"
            f" ({mins} min down)",
            "The in-flight turn died with the model. "
            "Continue from where you left off.",
            "If unsure, check git status and the last output first.",
        ]
    elif frozen_minutes is not None:
        lines.append(
            f"[cc-continuity watchdog] Turn {frozen_minutes} min frozen — "
            f"I cancelled it. check git status and the last output, "
            f"Continue from where you left off.")
    elif stalled:
        lines.append("[cc-continuity watchdog] "
                     "The last turn ended in error — continue from where you left off.")
    else:
        lines.append(f"[cc-continuity watchdog] The turn stalled "
                     f"({idle_minutes} min idle) — "
                     f"Continue from where you left off.")
    return "\n".join(lines)


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
    a model call, so DOWN/UNKNOWN waits (P10-D7.1). EXCEPTION (Phase 19,
    daemon restart): a FRESH episode (``n == 0``) with PENDING input →
    SEND_ENTER — the nudge was sent before the restart (the ledger
    remembers, the in-memory episode does not) and a bare Enter submits
    it without re-pasting a duplicate. Afterwards: pending input →
    SEND_ENTER (a bare Enter is free even while DOWN); text absent →
    RESEND (counts as the next attempt) until ``n >= max_nudges`` →
    GIVE_UP (P10-D7.4). ``given_up`` → DONE until a human
    ``user_prompt``.
    """
    if ep.given_up:
        return DONE
    if ep.n == 0:
        if pane_pending:
            return SEND_ENTER
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


# --- exit-hang killer (Phase 21, cp-059 forensics design) --------------------

#: The real ``/exit`` marker: a ``user`` entry whose RAW STRING content
#: starts with this tag. Verified across 109 transcripts — a bare
#: substring scan false-positives on transcripts that QUOTE the tag in
#: assistant text or embedded user text, so the rule is user-only +
#: startswith (never substring, never ``json.dumps``).
EXIT_MARKER = "<command-name>/exit</command-name>"
#: Post-hang ceremony: the user local-command output/caveat lines
#: (farewell, ...) written at the marker's own timestamp.
EXIT_CEREMONY_PREFIXES = ("<local-command-stdout>",
                          "<local-command-caveat>")
#: The assistant synthetic line CC writes seconds after a hung /exit.
EXIT_SYNTHETIC = "No response requested."


def parse_entry_ts(ts: str) -> float | None:
    """A transcript ``timestamp`` (ISO-8601, ``…Z``) → epoch seconds.

    Malformed → None. A NAIVE ts (no timezone) is malformed too:
    reading it as local time would silently return a WRONG epoch, and a
    wrong marker age would kill (or spare) the wrong process.
    """
    if not isinstance(ts, str) or not ts:
        return None
    body = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(body)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def transcript_slug(cwd: str) -> str:
    """Project cwd → the directory name CC uses under
    ``<config>/projects/``: every non-alphanumeric character becomes
    ``-`` (verified against real dirs: ``/Users/x/.claude`` →
    ``-Users-x--claude``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_exit_marker_ts(entries) -> float | None:
    """Ts of the NEWEST real /exit marker, or None.

    ``entries``: list of ``(ts, etype, text)`` tuples extracted by the
    daemon from the transcript. Marker rule (109-transcript forensics):
    ``etype == "user"`` AND text STARTS WITH :data:`EXIT_MARKER`.
    Assistant entries and user entries that merely quote the tag are
    never markers. A retried /exit simply yields a newer ts.
    """
    best = None
    for ts, etype, text in entries:
        if etype == "user" and text.startswith(EXIT_MARKER):
            if best is None or ts > best:
                best = ts
    return best


def _is_exit_ceremony(etype: str, text: str) -> bool:
    """True for the ONLY entry shapes a HUNG CC writes after the
    marker (forensics, every hung case): the local-command
    stdout/caveat farewell lines and the assistant's synthetic
    response line. Anything else after the marker is real activity."""
    if etype == "user":
        return text.startswith(EXIT_CEREMONY_PREFIXES)
    if etype == "assistant":
        return text.strip() == EXIT_SYNTHETIC
    return False


def exit_hang_due(entries, now: float, limit_s: float,
                  alive: bool = True) -> bool:
    """True when a live CC process has hung on /exit past the grace.

    ``entries``: list of ``(ts, etype, text)``. Decision (cp-059
    design): the NEWEST /exit marker is strictly older than
    ``limit_s`` (``≤`` is still within the grace — the user may retry
    or send a prompt), AND no real activity entry has landed after it.
    Real activity = anything that is NOT post-hang ceremony — including
    a tool-only assistant turn with empty text (a retry / a prompt
    arriving during a long TTFT keeps the process alive).
    ``alive=False``: the process is already gone → nothing to kill.
    """
    marker_ts = find_exit_marker_ts(entries)
    if marker_ts is None or not alive:
        return False
    if now - marker_ts <= limit_s:
        return False
    for ts, etype, text in entries:
        if ts > marker_ts and not _is_exit_ceremony(etype, text):
            return False
    return True


def find_claude_pid(root_pid: int, procmap: dict) -> int | None:
    """The TOPMOST ``claude`` binary in the pane's process tree.

    ``procmap``: ``{pid: (ppid, command)}`` from the daemon's
    ``ps_tree()``. BFS from ``root_pid`` INCLUSIVE — with the sh exec
    optimization the pane's sh wrapper can be claude itself — and the
    first process whose command's basename is exactly ``claude`` wins
    (path form ``/opt/…/claude`` matches; ``claude-hud`` does not).
    None when absent or when the root is not in the map.
    """
    if root_pid not in procmap:
        return None
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cmd) in procmap.items():
        children.setdefault(ppid, []).append(pid)
    queue = [root_pid]
    seen = {root_pid}
    while queue:
        current, queue = queue, []
        for pid in current:
            _ppid, command = procmap[pid]
            head = command.split()
            if head and os.path.basename(head[0]) == "claude":
                return pid
            for child in children.get(pid, ()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
    return None
