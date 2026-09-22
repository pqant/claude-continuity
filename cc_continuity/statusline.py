"""Status line entry — context metric (PRD §7).

Reads the status line JSON from stdin and:
1. computes ``used_tokens = input + cache_creation + cache_read`` (CC's
   ``used_percentage`` formula); falls back to ``context_window.
   total_input_tokens`` when ``current_usage`` is null; writes nothing when
   both are missing or when the payload has no ``session_id`` (subagent
   payloads carry ``agent_id`` and are ignored — they have their own windows);
2. atomically writes ``runtime/<session_id>.json`` (the metric the hooks
   consume; anything older than 10 minutes is stale, PRD §7);
3. chains the user's previous status line command (``chained_statusline`` in
   config, saved by the installer) with the *same* stdin and a 2 s timeout,
   then appends our segment: ``⟳ 61% ok`` / ``⟳ 63% PENDING→proj-002`` /
   ``⟳ 73% HANDOFF!`` / ``⟳ off``.

``window_tokens`` comes from config; when no config file sets it, the
``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` environment value is the default (else
262144). ``context_window_size`` is recorded but never trusted — ``doctor``
warns when it differs from the configured window. Our own work stays
< 50 ms; the chained command may take up to its 2 s timeout.
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

from . import atomic, config, ledger, naming, paths

#: Hooks treat a metric older than this as unknown (PRD §7, METRIC_STALE).
STALE_SECONDS = 600

_USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens")


def extract_metric(payload: dict | None, window_tokens: int) -> dict | None:
    """Build the runtime metric from one status line payload.

    Returns None when there is nothing usable to write (no payload, subagent
    payload, missing ``session_id``, or no token data at all).
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("agent_id"):  # subagent: has its own context window
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    cw = payload.get("context_window")
    cw = cw if isinstance(cw, dict) else {}
    usage = cw.get("current_usage")
    if isinstance(usage, dict):
        used = sum(usage.get(k, 0) for k in _USAGE_FIELDS)
    else:  # null/missing current_usage → fallback (PRD §7)
        total = cw.get("total_input_tokens")
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            return None
        used = total

    ratio = round(used / window_tokens, 4) if window_tokens else 0.0
    workspace = payload.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    return {
        "session_id": session_id,
        "ts": time.time(),
        "used_tokens": used,
        "window_tokens": window_tokens,
        "ratio": ratio,
        "cc_used_percentage": cw.get("used_percentage"),
        "project_dir": workspace.get("project_dir") or payload.get("cwd"),
        "transcript_path": payload.get("transcript_path"),
        "cc_version": payload.get("version"),
        "cc_context_window_size": cw.get("context_window_size"),
    }


def write_metric(metric: dict) -> Path:
    """Atomically write ``runtime/<session_id>.json``; returns the path."""
    path = paths.runtime_dir() / f"{metric['session_id']}.json"
    atomic.atomic_write_json(path, metric)
    return path


def read_metric(session_id: str) -> dict | None:
    """The latest metric for ``session_id``, or None (absent/corrupt)."""
    path = paths.runtime_dir() / f"{session_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_stale(metric: dict | None, now: float | None = None) -> bool:
    """True when the metric is missing or older than 10 minutes (PRD §7)."""
    if metric is None:
        return True
    ts = metric.get("ts")
    if not isinstance(ts, (int, float)):
        return True
    if now is None:
        now = time.time()
    return now - ts > STALE_SECONDS


# ----------------------------------------------- session lookup + segment

#: States that render as "HANDOFF!" (handoff requested or checkpoint written).
_HANDOFF_STATES = frozenset({
    "HANDOFF_REQUESTED", "HANDOFF_FAILED", "CHECKPOINT_WRITING",
    "CHECKPOINT_VERIFIED",
})


# ------------------------------------ Phase 11: color + shimmer sweep
#
# Effect source: ``ultrathink.sh`` (repo root) — the reverse-engineered
# Claude Code "ultrathink" rainbow shimmer: a 3-char bright band gliding
# left-to-right over per-char colored text, 50 ms tick, a 10-cell rest
# before and after each sweep. Here the band is driven by the wall clock
# instead of a timer, because CC re-runs the status line command itself;
# every render is the sweep's exact position at that instant.

#: Shimmer timing (ultrathink.sh): 50 ms tick, 10-cell rest each side.
SHIMMER_TICK_S = 0.05
SWEEP_PAD = 10
SHIMMER_BAND = 1  # chars within +/-1 of the glimmer index get the bright variant

ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"


#: (base, shimmer) basic-16 SGR pairs per live segment kind (P11-D2):
#: normal + BRIGHT variant of the same hue — the ultrathink.sh
#: base/shimmer concept in a palette that survives 16-color client
#: down-conversion (24-bit green quantized to GRAY on a 16-color tmux
#: client — observed 2026-09-22, user screenshot). Static = off/unknown.
_SHIMMER_COLORS = {
    "ok":       ("\x1b[32m", "\x1b[92m"),  # green / bright green
    "pending":  ("\x1b[33m", "\x1b[93m"),  # yellow / bright yellow
    "rotating": ("\x1b[36m", "\x1b[96m"),  # cyan / bright cyan
    "amcl":     ("\x1b[35m", "\x1b[95m"),  # magenta / bright magenta
    "handoff":  ("\x1b[31m", "\x1b[91m"),  # red / bright red
}
_BLOCKED_BASE = "\x1b[31m"  # red, own bold span
_COLOR_STATIC = "\x1b[90m"         # dim gray


def shimmer_index(now: float, length: int) -> int:
    """Glimmer cell for wall-clock ``now`` (ultrathink.sh math).

    ``glimmer = -SWEEP_PAD + floor(now / 50ms) % (length + 2*SWEEP_PAD)`` —
    the band starts one rest-width left of the text, sweeps one cell per
    tick, and rests one rest-width past it; a length-8 segment therefore
    sweeps 0.4 s and rests 1.0 s per cycle.
    """
    frame = int(now // SHIMMER_TICK_S) % (length + 2 * SWEEP_PAD)
    return frame - SWEEP_PAD


def _style(kind: str, text: str, blocked: str | None, now: float) -> str:
    """Wrap one plain segment in ANSI (ultrathink.sh shimmer, P11-D3).

    Live kinds: every char gets its kind's base color; the 3-char band
    (chars within +/-1 of the glimmer index) gets the brighter shimmer
    variant — a soft gliding highlight, never a blank frame. Bold, like
    the original. ``static`` (off/unknown): dim gray, never shimmers. The
    BLOCKED suffix keeps its own bold-red span, never shimmering.
    """
    blocked_part = (f"{ANSI_BOLD}{_BLOCKED_BASE}{blocked}{ANSI_RESET}"
                    if blocked else "")
    if kind == "static":
        return _COLOR_STATIC + text + ANSI_RESET + blocked_part
    base, shimmer = _SHIMMER_COLORS[kind]
    glimmer = shimmer_index(now, len(text))
    chars = "".join((shimmer if abs(i - glimmer) <= SHIMMER_BAND else base) + ch
                    for i, ch in enumerate(text))
    return ANSI_BOLD + chars + ANSI_RESET + blocked_part


def find_session_by_id(session_id: str | None) -> dict | None:
    """Scan ``projects/*/sessions/*/state.json`` for a matching session.

    Returns the state record, or None when there is no match (e.g. before
    Phase 3 registers sessions, or for an unknown id).
    """
    if not session_id:
        return None
    base = paths.projects_dir()
    if not base.is_dir():
        return None
    for state_file in base.glob("*/sessions/*/state.json"):
        try:
            rec = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("session_id") == session_id:
            return rec
    return None


def render_segment(ratio: float | None, record: dict | None,
                   enabled: bool, blocked: str | None = None,
                   *, ansi: bool = True,
                   now: float | None = None) -> str:
    """Render our ``⟳ …`` segment (PRD §7; Phase 11 styling, P11-D2/D3/D4).

    Forms: ``⟳ off`` (disabled / session off), ``⟳ …`` (no metric),
    ``⟳ NN% ok``, ``⟳ NN% PENDING→<successor>``, ``⟳ NN% ROTATING``,
    ``⟳ NN% AMCL``, ``⟳ NN% HANDOFF!``.
    The percentage is an int via ceil (never understate: the PRD pairs
    ratio 0.6036 with ``⟳ 61%``); the inner round kills IEEE-754 noise so
    ``0.07`` renders ``7%``, not ``8%``.
    ``⛔ BLOCKED`` suffix (Phase 9, P9-P5): machine-side truth while a
    needs_human ledger entry is unconsumed — visible on any later attach
    (spec §7 risk note).
    Phase 11: with ``ansi`` (default, config ``statusline_ansi``) live
    segments are state-colored with the ultrathink.sh shimmer sweep — a
    3-char bright band gliding across the text, position derived from the
    wall clock (``now`` injected for tests); ``off``/``…`` are static dim
    gray. ``ansi=False`` reproduces the legacy plain strings byte-for-byte.
    """
    now = time.time() if now is None else now
    blocked_text = f" ⛔ BLOCKED" if blocked else None
    if not ansi:
        if not enabled or (record is not None and record.get("off")):
            return "⟳ off"
        if ratio is None:
            return "⟳ …" + (blocked_text or "")
        pct = math.ceil(round(ratio * 100, 6))
        state_name = (record or {}).get("state", "ACTIVE")
        if state_name == "ROTATION_PENDING":
            successor = naming.successor_name(record.get("name", ""))
            return f"⟳ {pct}% PENDING→{successor}" + (blocked_text or "")
        if state_name == "ROTATING":
            return f"⟳ {pct}% ROTATING" + (blocked_text or "")
        if state_name == "AWAITING_MANUAL_CLEAR":
            return f"⟳ {pct}% AMCL" + (blocked_text or "")
        if state_name in _HANDOFF_STATES:
            return f"⟳ {pct}% HANDOFF!" + (blocked_text or "")
        return f"⟳ {pct}% ok" + (blocked_text or "")

    if not enabled or (record is not None and record.get("off")):
        return _style("static", "⟳ off", None, now)
    if ratio is None:
        return _style("static", "⟳ …", blocked_text, now)
    pct = math.ceil(round(ratio * 100, 6))
    state_name = (record or {}).get("state", "ACTIVE")
    if state_name == "ROTATION_PENDING":
        successor = naming.successor_name(record.get("name", ""))
        return _style("pending", f"⟳ {pct}% PENDING→{successor}",
                      blocked_text, now)
    if state_name == "ROTATING":
        return _style("rotating", f"⟳ {pct}% ROTATING", blocked_text, now)
    if state_name == "AWAITING_MANUAL_CLEAR":
        return _style("amcl", f"⟳ {pct}% AMCL", blocked_text, now)
    if state_name in _HANDOFF_STATES:
        return _style("handoff", f"⟳ {pct}% HANDOFF!", blocked_text, now)
    return _style("ok", f"⟳ {pct}% ok", blocked_text, now)


# ---------------------------------------------------------------- chaining


def chain(command: str | None, stdin_bytes: bytes,
          timeout: float = 2.0) -> str:
    """Run the user's previous status line command and return its stdout.

    The same stdin is forwarded. A missing/empty command yields ``""``. A
    timeout, spawn failure, or non-zero exit yields ``""`` — chaining must
    never break the status line. Output is stripped of surrounding
    whitespace so our segment appends cleanly.
    """
    if not command:
        return ""
    try:
        proc = subprocess.run(
            command, shell=True, input=stdin_bytes,
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def compose_line(chained: str, segment: str) -> str:
    """Join the chained user output and our segment (space-separated)."""
    if chained:
        return f"{chained} {segment}"
    return segment


# ------------------------------------------------------------------- entry


def run(stdin_bytes: bytes, cfg: dict) -> str:
    """Full status line pipeline (PRD §7). Returns the line to print.

    Parse the payload, write the metric (unless disabled), look up the
    session to know its state, render our segment, and append it after the
    chained user output. Never raises.
    """
    try:
        raw = stdin_bytes.decode("utf-8", "replace") if isinstance(
            stdin_bytes, (bytes, bytearray)) else str(stdin_bytes)
        payload = json.loads(raw) if raw.strip() else None
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        payload = None

    enabled = config.is_enabled(cfg)
    metric = None
    record = None
    if enabled:
        window = cfg.get("window_tokens") or 0
        metric = extract_metric(payload, window)
        if metric is not None:
            write_metric(metric)
        if payload is not None:
            record = find_session_by_id(payload.get("session_id"))

    ratio = metric["ratio"] if metric is not None else None
    blocked = None
    if record is not None:
        try:
            blocked = ledger.blocked_reason(
                paths.session_dir(record["project_key"],
                                  int(record["nnn"])))
        except (OSError, ValueError):
            blocked = None
    segment = render_segment(ratio, record, enabled, blocked=blocked,
                             ansi=bool(cfg.get("statusline_ansi", True)))
    chained = chain(cfg.get("chained_statusline"), stdin_bytes)
    return compose_line(chained, segment)
