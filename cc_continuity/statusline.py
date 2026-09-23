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
import os
import re
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
# left-to-right over per-char colored text. Here the band is driven by the
# wall clock instead of a timer, because CC re-runs the status line command
# itself; every render is the sweep's exact position at that instant.
# Revision 5 (2026-09-22): the file's 10-cell rest frames are dropped —
# at the status line's re-render cadence they made the band visible on
# only ~36% of renders (random on/off "shimmering", user screenshot);
# the band is always visible and glides per tick.
# Revision 9 (2026-09-23): the effect is ported to its ORIGINAL axis —
# 24-bit RGB, brighter same-hue band, 50 ms tick (ultrathink.sh,
# verbatim). Three basic-16 encodings were rejected by the user's eyes
# (9x bright: theme-eaten; reverse video: "okunmuyor bile"; hue shift:
# "ne oldugu belirsiz, cooook agir" — a shifted hue on a rainbow has no
# identity). The 24-bit port is proven on the user's chain: a tmux 3.7b
# truecolor client emits 38;2 SGR for hex status styles (pty capture
# 2026-09-23) and the user's eyes read the app's own 24-bit input-box
# rainbow as "renkli" (the 09-22 "quantized to gray" was the old 0a9b
# client — different era).

#: Shimmer timing (revision 9): 50 ms tick — the real CC cadence, 3x
#: faster than the 150 ms the user read as "cooook agir". NO rest frame
#: (revision 5): a rest would leave the band off on ~64% of renders at
#: this surface's discrete re-render cadence — the random on/off flicker
#: the user screenshotted; the band is always visible while the keyword
#: is in the prompt (revision 4's reactive gate is unchanged).
SHIMMER_TICK_S = 0.05
SHIMMER_BAND = 1  # chars within +/-1 of the glimmer index get the band style

ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"


#: Per-character rainbow (ultrathink.sh, verbatim, revision 9): 7 BASE
#: and 7 SHIMMER 24-bit SGR codes, cycled ``i % 7`` across the live
#: segment's chars — the reverse-engineered Claude Code "ultrathink"
#: 7-color rainbow. Base = the static per-char hue; SHIMMER = the
#: BRIGHTER SAME-HUE variant (the band, revision 9: luminance contrast
#: is the axis the user's display chain actually shows — 9x bright is
#: theme-eaten, hue shift is identity-less, reverse video reads as
#: breakage). The 09-22 "24-bit quantized to gray" observation was the
#: old 0a9b 16-color client; the current chain is proven truecolor
#: (pty-captured 38;2 SGR from a tmux truecolor client + the user's eyes
#: on the app's own 24-bit input-box rainbow, 2026-09-23).
_RAINBOW = [
    "\x1b[38;2;235;95;87m",   # red
    "\x1b[38;2;245;139;87m",  # orange
    "\x1b[38;2;250;195;95m",  # yellow
    "\x1b[38;2;145;200;130m", # green
    "\x1b[38;2;130;170;220m", # blue
    "\x1b[38;2;155;130;200m", # indigo
    "\x1b[38;2;200;130;180m", # violet
]
_SHIMMER = [
    "\x1b[38;2;250;155;147m",  # red_shimmer
    "\x1b[38;2;255;185;137m",  # orange_shimmer
    "\x1b[38;2;255;225;155m",  # yellow_shimmer
    "\x1b[38;2;185;230;180m",  # green_shimmer
    "\x1b[38;2;180;205;240m",  # blue_shimmer
    "\x1b[38;2;195;180;230m",  # indigo_shimmer
    "\x1b[38;2;230;180;210m",  # violet_shimmer
]
_BLOCKED_BASE = "\x1b[31m"  # red, own bold span
_COLOR_STATIC = "\x1b[90m"         # dim gray


def shimmer_index(now: float, length: int) -> int:
    """Glimmer cell for wall-clock ``now`` (ultrathink.sh math, revision 9).

    ``glimmer = floor(now / 50ms) % length`` — the band sits on the text
    at ALL times (no rest frame) and glides one cell per tick, wrapping at
    the end. The ultrathink.sh rest frames are deliberately dropped: the
    status line re-renders only on UI events (11-13 s while the model runs,
    per keystroke while typing), so a rest frame left the band off on ~64%
    of renders — the random on/off "shimmering" the user screenshotted
    (2026-09-22). Always-on is the non-straining form: the highlight never
    disappears and only moves while the user types (revision 4's reactive
    gate is unchanged).
    """
    return int(now // SHIMMER_TICK_S) % length


def _style(kind: str, text: str, blocked: str | None, now: float,
           *, shimmer: bool = False) -> str:
    """Wrap one plain segment in ANSI (ultrathink.sh rainbow shimmer).

    Live kinds (any state): a per-character 24-bit RAINBOW — char ``i``
    gets ``_RAINBOW[i % 7]`` (ultrathink.sh's loop, verbatim). With
    ``shimmer`` (Phase 11 rev 4: the prompt contains ``ultrathink``):
    the 3-char band (chars within +/-1 of the wall-clock glimmer index)
    renders ``_SHIMMER[i % 7]`` — the BRIGHTER SAME-HUE variant of the
    char's own base (revision 9; the real CC axis — luminance contrast.
    Rejected on this chain: 9x bright theme-eaten, reverse video read as
    corrupted text, hue shift identity-less) — repositioned on every CC
    re-render (the status line is a static string; it cannot self-animate).
    Without it: the static rainbow (all base hues — visually the sweep's
    rest frame). Bold, like the original. State is conveyed by the text
    itself, not by a single hue. ``static`` (off/unknown): dim gray, never
    shimmers. The BLOCKED suffix keeps its own bold-red span, never
    shimmering.
    """
    blocked_part = (f"{ANSI_BOLD}{_BLOCKED_BASE}{blocked}{ANSI_RESET}"
                    if blocked else "")
    if kind == "static":
        return _COLOR_STATIC + text + ANSI_RESET + blocked_part
    if not shimmer:
        chars = "".join(_RAINBOW[i % len(_RAINBOW)] + ch
                        for i, ch in enumerate(text))
        return ANSI_BOLD + chars + ANSI_RESET + blocked_part
    glimmer = shimmer_index(now, len(text))
    n = len(_RAINBOW)
    chars = "".join(
        ((_SHIMMER[i % n]
          if abs(i - glimmer) <= SHIMMER_BAND
          else _RAINBOW[i % n]) + ch)
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
                   now: float | None = None,
                   shimmer: bool = False) -> str:
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
    Phase 11 (revision 4): with ``ansi`` (default, config
    ``statusline_ansi``) live segments render the ultrathink.sh
    per-character 24-bit rainbow (7 base hues cycled ``i % 7`` —
    revision 9); ``shimmer`` (the prompt contains ``ultrathink`` —
    Phase 11 rev 4) activates the 3-char band (brighter same-hue 24-bit
    shimmer variant — revision 9),
    position derived from the wall clock (``now`` injected for tests),
    repositioned on every CC re-render. Default: static rainbow.
    ``off``/``…`` are static dim gray. ``ansi=False`` reproduces the
    legacy plain strings byte-for-byte.
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
                      blocked_text, now, shimmer=shimmer)
    if state_name == "ROTATING":
        return _style("rotating", f"⟳ {pct}% ROTATING", blocked_text, now,
                      shimmer=shimmer)
    if state_name == "AWAITING_MANUAL_CLEAR":
        return _style("amcl", f"⟳ {pct}% AMCL", blocked_text, now,
                      shimmer=shimmer)
    if state_name in _HANDOFF_STATES:
        return _style("handoff", f"⟳ {pct}% HANDOFF!", blocked_text, now,
                      shimmer=shimmer)
    return _style("ok", f"⟳ {pct}% ok", blocked_text, now, shimmer=shimmer)


# ------------------------ Phase 11 rev 4: reactive shimmer detection
#
# The bright band is live only while the pane's input box contains the
# word "ultrathink" (the user's prompt, as typed — a hook would only fire
# on submit). Detection reads the pane, never the conversation.


#: Word-boundary, case-insensitive — ultrathink.sh's ``/\bultrathink\b/gi``.
ULTRATHINK_RE = re.compile(r"\bultrathink\b", re.IGNORECASE)

#: CC's input box bottom border is a line of these (verified live, 014).
_INPUT_BORDER = "─"


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def _input_box_lines(timeout: float = 1.0) -> list[str] | None:
    """The pane's input-box region, or None on any failure.

    Phase 11 rev 4: ``tmux capture-pane -p`` on the current pane; the
    region is the last ``❯``-prefixed line through the next line
    consisting solely of ``─`` (CC's input box; a wrapped prompt stays
    inside it). Scrollback is never part of the region. Every failure
    path (no ``$TMUX``, spawn error, timeout, non-zero exit, no ``❯``
    line) returns None: detection must never break the status line.
    """
    if not os.environ.get("TMUX"):
        return None
    try:
        proc = subprocess.run([_tmux_bin(), "capture-pane", "-p"],
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("❯"):
            start = i
    if start is None:
        return None
    region = []
    for line in lines[start:]:
        region.append(line)
        stripped = line.strip()
        if stripped and set(stripped) == {_INPUT_BORDER}:
            break
    return region


def prompt_has_ultrathink(timeout: float = 1.0) -> bool:
    """True while the pane's input box contains the word ``ultrathink``.

    Phase 11 rev 4: searches ONLY the input-box region
    (``_input_box_lines``) — the last ``❯``-prefixed line through the
    next all-``─`` border line. Scrollback never triggers; every failure
    path returns False: detection must never break the status line.
    """
    region = _input_box_lines(timeout)
    return bool(region) and bool(
        ULTRATHINK_RE.search("\n".join(region)))


# ------------------------- render diagnostic log (2026-09-23 bug hunt)
#
# One line per ``run()`` into ``runtime/statusline-render.log``:
# ``<ts> live=<01> ansi=<01> ut=<01> glim=<n|-> box='<first ❯ line>'``.
# This is the evidence the shimmer bug hunt needs from LIVE renders —
# the real re-render cadence, the gate state, and exactly what the gate
# saw — without guessing. Append-only, size-capped, and it must never
# raise (the status line survives any log failure).

_RENDER_LOG_NAME = "statusline-render.log"
_RENDER_LOG_MAX_BYTES = 32 * 1024
_RENDER_LOG_KEEP_LINES = 500


def _render_log(enabled: bool, ansi: bool, live: bool, shimmer: bool,
                now: float, region: list[str] | None, segment: str) -> None:
    """Append this render's diagnostic line; swallow every failure."""
    try:
        if shimmer:
            # [0-9;] — the 24-bit SGR (38;2;R;G;B) carries semicolons;
            # \d+ alone would keep the escapes in `visible` and skew the
            # glimmer length (revision 9)
            visible = re.sub(r"\x1b\[[0-9;]*m", "", segment)
            glim = str(shimmer_index(now, max(len(visible), 1)))
        else:
            glim = "-"
        box = region[0][:48] if region else "-"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        path = paths.runtime_dir() / _RENDER_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts} live={int(live)} ansi={int(ansi)}"
                    f" ut={int(shimmer)} glim={glim} box='{box}'\n")
        if path.stat().st_size > _RENDER_LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(
                "\n".join(lines[-_RENDER_LOG_KEEP_LINES:]) + "\n",
                encoding="utf-8")
    except Exception:  # the status line must never break on logging
        pass


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
    ansi = bool(cfg.get("statusline_ansi", True))
    live = (enabled and ratio is not None
            and not (record is not None and record.get("off")))
    # Phase 11 rev 4: bright band only while the prompt (the pane's input
    # box) contains "ultrathink"; a static/off segment never reads the
    # pane (short-circuit). One capture feeds both the gate and the
    # render diagnostic log.
    now = time.time()
    region = _input_box_lines() if (ansi and live) else None
    shimmer = bool(region) and bool(
        ULTRATHINK_RE.search("\n".join(region)))
    segment = render_segment(ratio, record, enabled, blocked=blocked,
                             ansi=ansi, now=now, shimmer=shimmer)
    _render_log(enabled, ansi, live, shimmer, now, region, segment)
    chained = chain(cfg.get("chained_statusline"), stdin_bytes)
    return compose_line(chained, segment)
