"""Phase 13 digest relay: the watchdog log made visible in Claude Code.

The user's cron request: when the DGX is down, the watchdog ACTS (network
recovery, auto power-cycle) but its log was never visible inside a Claude
Code session — so the actions went unseen. A launchd PERIODIC agent
(StartInterval, not KeepAlive) runs ``cc-continuity watchdog digest run``
every ~2 min: it reads the new JSONL entries of ``logs/watchdog.log``
since the last processed ts, keeps the NOTABLE ones (health != UP, or a
dgx_down/dgx_up/power_cycle* event, or a network recovery that ran), and
shows a short digest (≤ 8 lines) via ``tmux display-popup`` on the ONE
most-recently-active live session's client. Phase 19: the digest is a
LOG, not a prompt — visible ~8 s, auto-closed by the daemon, and the
input box is NEVER touched, so it can never become a queued model turn
(the Phase 13 paste-buffer+Enter injection is gone). Popups CANNOT
display on a control client (tmux -CC — tmux's own regress test); with
no attached client the digest is file-logged only. Every digest is ALSO
appended to ``runtime/watchdog-digest.log`` regardless of display, so
the history is always inspectable. Displays are capped at one per 5 min
(config ``watchdog_digest_push_cap_s``) — notable entries keep being
file-logged inside the window, they just do not consume a popup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config, notify, paths
from .watchdogd import (NUDGE_ELIGIBLE, collect_live_sessions,
                        last_ledger_ts, list_tmux_sessions, resolve_pane)

#: The LaunchAgent label (plist file name minus .plist).
PLIST_LABEL = "com.claude-continuity.watchdog-digest"
#: StartInterval for the periodic agent: the watchdog ticks every 300 s,
#: so 120 s catches every tick within one interval + one tick.
DEFAULT_START_INTERVAL_S = 120
#: At most one pane injection per this window (model turns are expensive).
DEFAULT_PUSH_CAP_S = 300.0
#: The digest is ≤ 8 lines: header + optional truncation note + at most
#: this many entry lines + footer.
MAX_ENTRY_LINES = 5
#: Footer: Phase 19 — the digest is a LOG (display-popup), not a prompt:
#: no ack is expected.
_DIGEST_FOOTER = ("(cc-continuity watchdog digest — log only, not a prompt)")
#: Phase 19: display-popup geometry and lifetime. Module constants for
#: v1 (the user asked "ayarlanabilir" — config keys can follow; see
#: docs/WATCHDOG.md).
POPUP_WIDTH = 110
POPUP_HEIGHT = 16
POPUP_DURATION_S = 8.0
POPUP_TITLE = "cc-continuity watchdog digest"
#: Event prefixes that repeat on EVERY tick for dead/stale sessions (the
#: chronic CAPTURE_FAILED open problem). They never trigger notable()
#: (it keys on health / dgx_* / power_cycle* / recovery.ran); they are
#: dropped from the DISPLAY so they cannot drown the outage signal.
_NOISY_EVENT_PREFIXES = ("capture_failed:", "unresolvable:")


class DigestError(Exception):
    """CLI-level failure (install/uninstall); the run never raises."""


# --- pure decision layer -------------------------------------------------------


def notable(entry: dict) -> bool:
    """True when a watchdog.log tick entry is worth the user's eyes.

    ``health != "UP"`` covers DOWN and the cold-start UNKNOWN (one failed
    probe after a daemon restart — an early warning) and the error tick
    (no health key at all); plus a healthy tick that still did something:
    a dgx_down/dgx_up/power_cycle/power_cycle_failed* event, or a network
    recovery that actually ran."""
    if entry.get("health") != "UP":
        return True
    for ev in entry.get("events") or []:
        s = str(ev)
        if s in ("dgx_down", "dgx_up", "power_cycle") \
                or s.startswith("power_cycle_failed"):
            return True
    rec = entry.get("recovery")
    return isinstance(rec, dict) and bool(rec.get("ran"))


def _entry_line(entry: dict) -> str:
    ts = float(entry.get("ts") or 0.0)
    line = (f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')} "
            f"health={entry.get('health')}")
    rec = entry.get("recovery")
    if isinstance(rec, dict) and rec.get("ran"):
        line += (f" recovery(exit={rec.get('exit')},"
                 f"{round(float(rec.get('elapsed_s') or 0.0))}s)")
    events = [str(e) for e in (entry.get("events") or [])
              if not str(e).startswith(_NOISY_EVENT_PREFIXES)]
    if events:
        line += f" ev=[{', '.join(events)}]"
    return line


def format_digest(entries: list[dict], now: float) -> str:
    """The injectable text: header + ≤ MAX_ENTRY_LINES entry lines +
    footer (≤ 8 lines total). ``entries`` in chronological order; when
    truncated, the MOST RECENT ones are shown (an old entry with no new
    information is least worth a line). Empty → "" (caller skips)."""
    if not entries:
        return ""
    lines = [f"[cc-continuity watchdog digest "
             f"{datetime.fromtimestamp(now).strftime('%H:%M')}]"]
    if len(entries) > MAX_ENTRY_LINES:
        lines.append(f"(showing {MAX_ENTRY_LINES} of {len(entries)} — "
                     f"full log: runtime/watchdog-digest.log)")
        entries = entries[-MAX_ENTRY_LINES:]
    lines.extend(_entry_line(e) for e in entries)
    lines.append(_DIGEST_FOOTER)
    return "\n".join(lines)


# --- state file -----------------------------------------------------------------


def state_path() -> Path:
    return paths.runtime_dir() / "watchdog-digest-state.json"


def digest_log_path() -> Path:
    return paths.runtime_dir() / "watchdog-digest.log"


def load_state(path: Path | str) -> dict:
    """{"last_ts": float|None, "last_push_ts": float|None}; missing or
    corrupt file → both None (first-run behavior; never raises)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_ts": None, "last_push_ts": None}
    if not isinstance(data, dict):
        return {"last_ts": None, "last_push_ts": None}
    out: dict = {}
    for k in ("last_ts", "last_push_ts"):
        v = data.get(k)
        out[k] = float(v) if isinstance(v, (int, float)) \
            and not isinstance(v, bool) else None
    return out


def save_state(path: Path | str, st: dict) -> None:
    """Atomic write (tmp + rename); never raises."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


# --- log reading ------------------------------------------------------------------


def read_new_entries(log_file: Path | str,
                     last_ts: float) -> list[dict]:
    """JSONL entries with ``ts > last_ts`` (chronological; a corrupt line
    is skipped, a missing file is empty — the relay must never raise,
    the same P10-D12 discipline as the daemon)."""
    out: list[dict] = []
    try:
        lines = Path(log_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) \
                and ts > last_ts:
            out.append(entry)
    return out


# --- push rate limit ----------------------------------------------------------------


def push_allowed(last_push_ts: float | None, now: float,
                 cap_s: float) -> bool:
    """At most one pane display per ``cap_s`` (None = never displayed).
    An UNsuccessful display does not arm the window — the caller only
    records ``last_push_ts`` when the popup landed."""
    if last_push_ts is None:
        return True
    return now - last_push_ts >= cap_s


# --- pane targeting ------------------------------------------------------------------


def _best_candidate(candidates: list[tuple[str, int, dict, float | None]]
                    ) -> tuple[float, str, int, dict] | None:
    """The one eligible, ledger-active candidate with the max
    ``last_ledger_ts`` (max among ACTIVE/WARNED; a None ts = no chain
    activity → stale, skipped; ties keep the first in sort order), as
    ``(last_ts, key, nnn, rec)`` — or None. Single source of truth shared
    by :func:`pick_target` and :func:`_target_turn_active`."""
    best = None  # (last_ts, key, nnn, rec)
    for key, nnn, rec, last_ts in candidates:
        if rec.get("state") not in NUDGE_ELIGIBLE:
            continue
        if not isinstance(last_ts, (int, float)) \
                or isinstance(last_ts, bool):
            continue
        if best is None or last_ts > best[0]:
            best = (last_ts, key, nnn, rec)
    return best


def _target_turn_active(candidates: list[tuple[str, int, dict, float | None]]
                        ) -> bool:
    """True when the session :func:`pick_target` would pick (the
    most-recently-active eligible candidate) is mid-turn
    (``turn_active``). run_once uses this to skip the display and retry
    on the next tick instead of showing the popup over a busy session —
    the popup steals keystrokes while it is up, and the user wants the
    digest ACTIONED, not arriving mid-turn."""
    best = _best_candidate(candidates)
    return best is not None and bool(best[3].get("turn_active"))


def pick_target(candidates: list[tuple[str, int, dict, float | None]],
                tmux_sessions: list[str] | None) -> str | None:
    """The ONE pane whose client shows the popup: the most-recently-active
    eligible session's pane (see :func:`_best_candidate`). Pane resolution
    is the daemon's ``resolve_pane`` (hook-recorded ``tmux_session``
    field, else exactly-one prefix match); None → the digest is
    file-logged but NOT displayed."""
    best = _best_candidate(candidates)
    if best is None:
        return None
    return resolve_pane(best[3], notify.project_slug(best[1]),
                        tmux_sessions)


# --- pane display (Phase 19: display-popup — a LOG, never a prompt) ---------------


def _pick_client(runner, tmux: str, target: str) -> str | None:
    """The client to display on: a client attached to ``target``
    (``list-clients -t``) wins; else ANY attached client; else None
    (popups cannot display on a control client — log-only). Never
    raises."""
    for cmd in ([tmux, "list-clients", "-t", target, "-F",
                 "#{client_name}"],
                [tmux, "list-clients", "-F", "#{client_name}"]):
        try:
            proc = runner(cmd)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            continue
        out = proc.stdout or b""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        for line in out.splitlines():
            if line.strip():
                return line.strip()
    return None


def show_popup(target: str, text: str, *,
               duration_s: float = POPUP_DURATION_S,
               runner=subprocess.run, tmux_bin: str | None = None,
               sleep=time.sleep) -> bool:
    """Phase 19: show the digest as a tmux display-popup — a LOG, never
    a prompt. The input box is not touched at all, so the digest can
    never become a queued model turn (the Phase 13 paste-buffer+Enter
    injection is gone).

    Mechanics (verified live on tmux 3.7c): the opener is a DIRECT
    :func:`subprocess.Popen` — the ``display-popup <cmd>`` CLI BLOCKS
    until the popup closes, so it cannot go through ``runner``. The
    popup shell ``cat <tmp>; sleep N`` keeps the popup up for
    ``duration_s``; the daemon then closes it early with
    ``display-popup -x`` (harmless no-op if it already closed) and
    reaps the opener. The client is picked via ``list-clients``
    (target-specific, else any); with NO client (control client /
    detached) the digest is file-logged only → False. The text rides a
    temp file (the popup shell cats it); the file is cleaned in
    ``finally``. Never raises; True only when the popup opened AND the
    opener exited 0."""
    tmux = (tmux_bin or os.environ.get("CC_CONTINUITY_TMUX_BIN")
            or "tmux")
    tmp = paths.runtime_dir() / "watchdog-digest-paste.txt"
    try:
        tmp.write_text(text, encoding="utf-8")
        client = _pick_client(runner, tmux, target)
        if client is None:
            return False
        opener = subprocess.Popen(
            [tmux, "display-popup", "-c", client, "-t", target,
             "-T", POPUP_TITLE, "-w", str(POPUP_WIDTH),
             "-h", str(POPUP_HEIGHT),
             f"cat {tmp}; sleep {duration_s:.0f}"])
        sleep(duration_s)
        runner([tmux, "display-popup", "-x", "-c", client])
        try:
            return opener.wait(timeout=duration_s + 10) == 0
        except subprocess.SubprocessError:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --- the one-shot run (launchd StartInterval / CLI) -------------------------------------


def run_once(*, now: float | None = None,
             log_file: Path | None = None,
             state_file: Path | str | None = None,
             digest_log: Path | str | None = None,
             list_sessions=None, collect=None, ledger_ts=None,
             display=None, cfg_over: dict | None = None) -> dict:
    """One digest pass. Returns a JSON-ready summary
    ``{"scanned", "notable", "target", "pushed", "skipped_busy"}`` and
    NEVER raises — the periodic agent must survive any failure
    (P10-D12 discipline).

    Steps: read new watchdog.log entries since the state's ``last_ts``
    (first run SEEDS to now — a week of history is not replayed); keep
    the notable ones; if any, format + ALWAYS append to
    ``runtime/watchdog-digest.log``; then, outside the push cap, show it
    as a ~8 s display-popup on the most-recently-active live session's
    client (Phase 19: a visible LOG that never touches the input box —
    a failed display, a missing pane, or no attached client records NO
    ``last_push_ts`` — the next run retries). If that target is
    mid-turn (``turn_active``), the display is SKIPPED (``skipped_busy``)
    and ``last_ts`` is held back (no advance) so the next 2-min tick
    re-reads the notable window and retries — the user wants the digest
    ACTIONED, not arriving mid-turn. Otherwise the state advances to the
    newest ts seen (notable or not) so quiet ticks are not re-scanned."""
    cfg = config.load_config()
    cfg.update(cfg_over or {})
    now = now if now is not None else time.time()
    if log_file is None:
        log_file = paths.logs_dir() / "watchdog.log"
    if state_file is None:
        state_file = state_path()
    if digest_log is None:
        digest_log = digest_log_path()
    if list_sessions is None:
        list_sessions = list_tmux_sessions
    if collect is None:
        collect = collect_live_sessions
    if ledger_ts is None:
        ledger_ts = last_ledger_ts
    if display is None:
        display = show_popup

    st = load_state(state_file)
    last_ts = st["last_ts"]
    if last_ts is None:
        last_ts = now  # seed: first run does not replay the backlog
    entries = read_new_entries(log_file, last_ts)
    new_last_ts = last_ts
    for e in entries:
        ts = e.get("ts")
        if isinstance(ts, (int, float)) and ts > new_last_ts:
            new_last_ts = ts
    notable_entries = [e for e in entries if notable(e)]

    target = None
    pushed = False
    skipped_busy = False
    if notable_entries:
        digest = format_digest(notable_entries, now)
        try:
            with open(digest_log, "a", encoding="utf-8") as f:
                f.write(digest + "\n")
        except OSError:
            pass
        cap = float(cfg.get("watchdog_digest_push_cap_s",
                            DEFAULT_PUSH_CAP_S))
        if push_allowed(st["last_push_ts"], now, cap):
            try:
                sessions = list_sessions()
            except Exception:
                sessions = None
            candidates = []
            try:
                for key, nnn, rec in collect():
                    sdir = paths.session_dir(key, nnn)
                    try:
                        ts = ledger_ts(sdir)
                    except Exception:
                        ts = None
                    candidates.append((key, nnn, rec, ts))
            except Exception:
                candidates = []
            target = pick_target(candidates, sessions)
            if target is not None:
                if _target_turn_active(candidates):
                    # Mid-turn protection (Phase 14, kept): a popup shown
                    # over a mid-turn session steals its keystrokes. Do
                    # NOT display it mid-turn — the user wants it ACTIONED
                    # now. Skip; last_ts is held back below so the next
                    # 2-min tick re-reads these entries and retries.
                    skipped_busy = True
                else:
                    try:
                        ok = display(target, digest)
                    except Exception:
                        ok = False
                    if ok:
                        pushed = True
                        st["last_push_ts"] = now

    # Consume every entry seen — EXCEPT when the target was mid-turn: hold
    # last_ts back (no advance) so the notable window is re-read on the next
    # tick and retried (the user's "retry next tick" spec). Re-scanning the
    # quiet entries before it is harmless.
    st["last_ts"] = last_ts if skipped_busy else new_last_ts
    save_state(state_file, st)
    return {"scanned": len(entries), "notable": len(notable_entries),
            "target": target, "pushed": pushed, "skipped_busy": skipped_busy}


# --- plist / launchctl (same pattern as power_plug / watchdogd) ----------------------


def plist_path(home: Path | str) -> Path:
    return (Path(home) / "Library" / "LaunchAgents"
            / f"{PLIST_LABEL}.plist")


def plist_render(binary: str, home: Path | str,
                 tmux_bin: str | None = None) -> str:
    """The LaunchAgent plist. PERIODIC, not resident: StartInterval
    every 120 s + RunAtLoad, NO KeepAlive (a crashed one-shot is
    re-spawned by the interval, not restarted immediately). The absolute
    ``cc-continuity`` AND ``tmux`` paths are baked at install time
    (launchd's minimal PATH — same lesson as the watchdog plist)."""
    logs = paths.logs_dir()
    tmux_env = ""
    if tmux_bin:
        tmux_env = (
            "  <key>EnvironmentVariables</key>\n  <dict>\n"
            "    <key>CC_CONTINUITY_TMUX_BIN</key>\n"
            f"    <string>{tmux_bin}</string>\n"
            "  </dict>\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key>\n  <string>{PLIST_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        f"    <string>{binary}</string>\n"
        "    <string>watchdog</string>\n    <string>digest</string>\n"
        "    <string>run</string>\n"
        "  </array>\n"
        f"{tmux_env}"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        f"  <key>StartInterval</key>\n"
        f"  <integer>{DEFAULT_START_INTERVAL_S}</integer>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{home}</string>\n"
        f"  <key>StandardOutPath</key>\n"
        f"  <string>{logs}/watchdog-digest.out</string>\n"
        f"  <key>StandardErrorPath</key>\n"
        f"  <string>{logs}/watchdog-digest.err</string>\n"
        "</dict>\n</plist>\n"
    )


def _launchctl(runner, *args: str) -> bool:
    try:
        proc = runner(["launchctl", *args], capture_output=True,
                      timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_plist(home: Path | str, *, runner=subprocess.run,
                  which=None) -> Path:
    """Resolve the installed binary, write the plist,
    ``launchctl bootstrap gui/<uid> <plist>``. Raises DigestError on
    failure (CLI-level). ``which`` defaults to ``shutil.which`` at call
    time (def-time would defeat the tests' monkeypatching)."""
    if which is None:
        which = shutil.which
    binary = which("cc-continuity")
    if binary is None:
        raise DigestError("cc-continuity not on PATH (install first)")
    p = plist_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plist_render(binary, Path(home),
                              tmux_bin=which("tmux")), encoding="utf-8")
    if not _launchctl(runner, "bootstrap", f"gui/{os.getuid()}", str(p)):
        raise DigestError(
            "launchctl bootstrap failed — see `cc-continuity doctor`")
    return p


def uninstall_plist(home: Path | str, *, runner=subprocess.run) -> None:
    _launchctl(runner, "bootout", f"gui/{os.getuid()}/{PLIST_LABEL}")
    plist_path(home).unlink(missing_ok=True)


def start_plist(home: Path | str, *, runner=subprocess.run) -> bool:
    """kickstart = run one pass NOW (then the interval takes over)."""
    return _launchctl(runner, "kickstart",
                      f"gui/{os.getuid()}/{PLIST_LABEL}")


def stop_plist(home: Path | str, *, runner=subprocess.run) -> bool:
    return _launchctl(runner, "bootout",
                      f"gui/{os.getuid()}/{PLIST_LABEL}")


def status_plist(home: Path | str, *, runner=subprocess.run) -> str | None:
    """``launchctl print gui/<uid>/<label>`` output (None when the
    agent is not loaded)."""
    try:
        proc = runner(["launchctl", "print",
                       f"gui/{os.getuid()}/{PLIST_LABEL}"],
                      capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if isinstance(out, bytes):  # fakes may answer with str
        out = out.decode(errors="replace")
    return out
