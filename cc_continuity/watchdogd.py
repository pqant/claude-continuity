"""Phase 10 daemon (P10-D12): the watchdog I/O loop.

``watchdog.py`` is the pure decision core; this module owns every
curl/tmux/ledger/store touch. Hard rule (P10-D12): the watchdog must
NEVER break the continuity loop — every I/O is wrapped; a tick
exception is one ``errors.log`` line and the loop continues.
``run_forever`` is the loop itself (``cc-continuity watchdog run`` —
used by the launchd plist AND by the integration tests).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import subprocess
import time
import traceback
from pathlib import Path

from . import ledger, notify, paths, power_plug, state
from .watchdog import (Episode, HealthState, build_nudge_text, classify,
                       exit_hang_due, find_claude_pid, find_exit_marker_ts,
                       nudge_action, parse_entry_ts, pane_pending,
                       power_cycle_due, transcript_slug)

#: P10-D2/P10-P8: the LaunchAgent label (plist file name minus .plist).
PLIST_LABEL = "com.claude-continuity.watchdog"

#: States whose sessions may receive a nudge (P10-D5).
NUDGE_ELIGIBLE = frozenset({"ACTIVE", "WARNED"})

#: Phase 19: pause between the force-cancel Escape and the nudge text —
#: long enough for the in-flight turn (and any stale input-box line) to
#: settle, short enough that a 5-min tick feels instant. Module constant
#: (the user called it "ayarlanabilir"; a config key can follow v1).
ESCAPE_SETTLE_S = 0.75

#: P10-P9 refinement (binding): the daemon's own ledger writes are NOT
#: chain activity — counting them would make every nudge look like a
#: recovery (fresh ledger → ALIVE) and the RESEND/GIVE_UP machine would
#: deadlock on the first attempt (see the Task 7 plan note).
_DAEMON_TYPES = frozenset({"watchdog_nudge", "watchdog_verified",
                           "watchdog_freeze"})


class WatchdogError(Exception):
    """CLI-level failure (install/uninstall); the daemon never raises."""


# --- logging (P10-D10) --------------------------------------------------------


def _log_line(entry: dict) -> None:
    """One JSONL line to ``logs/watchdog.log``. Never raises."""
    try:
        with open(paths.logs_dir() / "watchdog.log", "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _log_error(where: str) -> None:
    """One ``errors.log`` line (P10-D12: tick exception → log + go on)."""
    try:
        tb = traceback.format_exc()
        tail = tb.strip().splitlines()[-1] if tb else ""
        with open(paths.logs_dir() / "errors.log", "a",
                  encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] "
                    f"watchdog: {where} failed: {tail}\n")
    except OSError:
        pass


def _last_nonempty(capture: str) -> str | None:
    for line in reversed(capture.splitlines()):
        if line.strip():
            return line
    return None


def _push(cfg: dict, event: str, *, slug: str, nnn: int, **fields) -> bool:
    """notify.safe_send wrapper (best-effort; never raises)."""
    try:
        return notify.safe_send(cfg, event, slug=slug, nnn=nnn, **fields)
    except Exception:
        return False


# --- real I/O (the run_forever defaults) --------------------------------------


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def probe_health(cfg: dict) -> bool:
    """P10-D4: ``curl -s -o /dev/null -m 5 -w %{http_code} <url>`` →
    rc==0 AND body ``200``. Empty URL → True (liveness-only mode)."""
    url = str(cfg.get("watchdog_health_url") or "").strip()
    if not url:
        return True
    try:
        proc = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-m", "5",
             "-w", "%{http_code}", url],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and \
        proc.stdout.decode(errors="replace").strip() == "200"


# P10 (network recovery): the macOS root wrapper re-establishes the DGX route;
# it tears Ethernet down for ~15s, so the daemon runs it ONLY on a failed
# probe, never on a healthy one. See setup_dgx_network_recovery.sh.
_RECOVERY_TIMEOUT_S = 30


def recover_network(cfg: dict, runner=subprocess.run) -> dict:
    """Re-establish the DGX route after a failed probe, so a stale route
    (e.g. after a DGX/Mac restart) is not counted as a real failure.

    Runs ``cfg["watchdog_network_recovery_cmd"]`` via the shell; an empty /
    missing command is a no-op (``ran: False``, nothing executed). Captures
    combined stdout+stderr so the run is inspectable in Claude Code (the
    caller logs it). Never raises (P10-D12): a failed start degrades to
    ``ran: True, exit: None`` — the probe still gets its second chance, and
    if the route was not actually restored the next tick counts a real
    failure."""
    cmd = str(cfg.get("watchdog_network_recovery_cmd") or "").strip()
    if not cmd:
        return {"ran": False, "exit": None, "elapsed_s": 0.0, "out": ""}
    start = time.monotonic()
    try:
        proc = runner(cmd, shell=True, capture_output=True,
                      timeout=_RECOVERY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return {"ran": True, "exit": None,
                "elapsed_s": time.monotonic() - start,
                "out": "recovery command failed to start"}
    out = (proc.stdout or b"").decode(errors="replace").strip()
    err = (proc.stderr or b"").decode(errors="replace").strip()
    return {"ran": True, "exit": proc.returncode,
            "elapsed_s": time.monotonic() - start,
            "out": "\n".join(p for p in (out, err) if p)}


def _recovery_log(cfg: dict, recovery: dict) -> None:
    """The full recovery output, timestamped, to
    ``runtime/watchdog-recovery.log`` — the script's ``[DGX] ...`` lines,
    inspectable inside Claude Code (the user's requirement). Never raises."""
    cmd = str(cfg.get("watchdog_network_recovery_cmd") or "").strip()
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        body = (f"{stamp} cmd={cmd or '(disabled)'} "
                f"exit={recovery.get('exit')} "
                f"elapsed_s={recovery.get('elapsed_s')}\n")
        out = str(recovery.get("out") or "").strip()
        if out:
            body += out + "\n"
        with open(paths.runtime_dir() / "watchdog-recovery.log", "a",
                  encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass


# --- auto power-cycle (Phase 13, design 7899) ---------------------------------

#: The localhost power-plug endpoint's /cycle holds for
#: power_cycle_off_s + two verified relay reads; the ceiling sits well
#: above any sane off duration so a slow relay is reported, not cut off.
_CYCLE_TIMEOUT_S = 60


def cycle_power(cfg: dict, runner=subprocess.run) -> dict:
    """POST ``/cycle`` to the localhost power-plug endpoint.

    Returns the endpoint's JSON (``{"ok": ..., "state": ..., ...}``);
    a dead endpoint / non-JSON reply degrades to
    ``{"ok": False, "state": "unknown", "error": ...}`` (P10-D12:
    never raises — the tick loop must survive)."""
    url = (f"http://{power_plug.ENDPOINT_HOST}:"
           f"{power_plug.ENDPOINT_PORT}/cycle")
    try:
        proc = runner(["curl", "-s", "-m", str(_CYCLE_TIMEOUT_S),
                       "-X", "POST", url],
                      capture_output=True,
                      timeout=_CYCLE_TIMEOUT_S + 10)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "state": "unknown",
                "error": "endpoint call failed"}
    try:
        res = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except ValueError:
        return {"ok": False, "state": "unknown",
                "error": "endpoint returned non-JSON"}
    if not isinstance(res, dict):
        return {"ok": False, "state": "unknown",
                "error": "endpoint returned non-object"}
    res.setdefault("state", "unknown")
    return res


def _power_cycle_state_file() -> Path:
    return paths.runtime_dir() / "power-cycle.json"


def load_last_cycle_ts() -> float | None:
    """ts of the last SUCCESSFUL auto power cycle, or None (missing /
    corrupt file). File-based so the cooldown survives daemon restarts
    (launchd KeepAlive) — otherwise a crash inside the 30-min window
    would arm an immediate re-cycle."""
    try:
        data = json.loads(
            _power_cycle_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ts = data.get("last_cycle_ts") if isinstance(data, dict) else None
    return float(ts) if isinstance(ts, (int, float)) else None


def save_last_cycle_ts(ts: float) -> None:
    """Atomic write (tmp + rename); never raises."""
    try:
        p = _power_cycle_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"last_cycle_ts": ts}),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def capture_pane(target: str) -> str | None:
    try:
        proc = subprocess.run(
            [_tmux_bin(), "capture-pane", "-p", "-t", target],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode(errors="replace") \
        if proc.returncode == 0 else None


def list_tmux_sessions() -> list[str] | None:
    """Session names, or None when the tmux server is unavailable."""
    try:
        proc = subprocess.run(
            [_tmux_bin(), "list-sessions", "-F", "#{session_name}"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [l for l in proc.stdout.decode(errors="replace").splitlines()
            if l.strip()]


def send_keys(target: str, *keys: str) -> bool:
    try:
        proc = subprocess.run(
            [_tmux_bin(), "send-keys", "-t", target, *keys],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# --- exit-hang killer (Phase 21) -------------------------------------------------
#
# CC 2.1.209 can hang on /exit: the marker entry is written but the
# process never dies. The 5s fast pass (check_exit_hangs) finds the
# hung pane's topmost claude process and kills it after the grace.
# No-op passes write ZERO log lines (the 5s cadence must not bloat the
# log); a kill is one ntfy push + one log line.

#: Phase 21: cadence of the fast pass inside run_forever.
EXIT_HANG_POLL_S = 5.0
#: Phase 21: default /exit grace in seconds (override:
#: ``CC_CONTINUITY_EXIT_HANG_LIMIT``).
EXIT_HANG_DEFAULT_LIMIT_S = 30.0
#: Phase 21: transcript tail size — a hung CC stops writing, so the
#: marker is always near the end; the head is never read.
_TRANSCRIPT_TAIL_BYTES = 512 * 1024
#: Phase 21: kill_process poll step between SIGTERM and the SIGKILL
#: deadline.
_KILL_POLL_S = 0.2


def exit_hang_limit_s() -> float:
    """Phase 21: the /exit grace in seconds (env
    ``CC_CONTINUITY_EXIT_HANG_LIMIT``). Empty / broken / ``<= 0`` falls
    back to the default — a 0/negative grace would kill a mid-/exit
    (the user may still retry)."""
    raw = os.environ.get("CC_CONTINUITY_EXIT_HANG_LIMIT", "").strip()
    if not raw:
        return EXIT_HANG_DEFAULT_LIMIT_S
    try:
        val = float(raw)
    except ValueError:
        return EXIT_HANG_DEFAULT_LIMIT_S
    return val if val > 0 else EXIT_HANG_DEFAULT_LIMIT_S


def kill_process(pid: int, *, term_wait_s: float = 5.0,
                 sleep=time.sleep, clock=time.monotonic,
                 kill=os.kill) -> str:
    """Phase 21: SIGTERM, wait ``term_wait_s``, then SIGKILL.

    Returns ``"term"`` (TERM won), ``"kill"`` (KILL was needed), or
    ``"dead"`` (already gone before the first signal — or a signal
    itself failed: EPERM etc. degrade to "dead", P10-D12 — a false
    "killed" push is worse than silence). NEVER raises."""
    try:
        kill(pid, 0)
    except OSError:
        return "dead"
    try:
        kill(pid, signal.SIGTERM)
    except OSError:
        return "dead"
    deadline = clock() + term_wait_s
    while clock() < deadline:
        sleep(_KILL_POLL_S)
        try:
            kill(pid, 0)
        except OSError:
            return "term"
    try:
        kill(pid, signal.SIGKILL)
    except OSError:
        return "term"  # at the deadline, TERM won
    return "kill"


def pid_alive(pid: int) -> bool:
    """Phase 21: liveness via ``os.kill(pid, 0)``. ProcessLookupError →
    False (gone). Any other OSError (EPERM — a process we cannot
    signal) → True (it exists; we just cannot touch it). A zombie also
    answers True — the killed-set in check_exit_hangs is the guard."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def ps_tree(runner=subprocess.run) -> dict | None:
    """Phase 21: the whole-machine process map ``{pid: (ppid, command)}``
    (``ps -axo pid=,ppid=,command=``). The command column is the REST of
    the line (it may contain spaces). rc != 0 / runner error → None
    (the fast pass degrades, it never raises); a malformed row is
    skipped."""
    try:
        proc = runner(["ps", "-axo", "pid=,ppid=,command="],
                      capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: dict[int, tuple[int, str]] = {}
    for row in proc.stdout.decode(errors="replace").splitlines():
        parts = row.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        out[pid] = (ppid, parts[2])
    return out


def list_tmux_panes(session: str,
                    runner=subprocess.run) -> list[dict] | None:
    """Phase 21: the session's panes as ``{"pane", "pid", "path"}``.
    The path column may contain spaces → split from the LEFT (first
    two tokens, the rest is the path). rc != 0 / runner error → None;
    a malformed row (pid not an int) is skipped."""
    try:
        proc = runner([_tmux_bin(), "list-panes", "-t", session, "-F",
                       "#{pane_id} #{pane_pid} #{pane_current_path}"],
                      capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    panes: list[dict] = []
    for row in proc.stdout.decode(errors="replace").splitlines():
        parts = row.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        panes.append({"pane": parts[0], "pid": pid, "path": parts[2]})
    return panes


def read_transcript_entries(cwd: str) -> list | None:
    """Phase 21: the newest transcript under
    ``<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<slug>`` as
    ``(ts, etype, text)`` rows. Only the TAIL (512 KB) is read — a hung
    CC stops writing, so the marker is always near the end (the partial
    first line after the seek is dropped, same rule as any bad row).
    No directory / no files / unreadable → None."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    d = Path(cfg_dir) / "projects" / transcript_slug(cwd)
    try:
        files = [f for f in d.glob("*.jsonl") if f.is_file()]
    except OSError:
        return None
    if not files:
        return None
    newest = max(files, key=lambda f: f.stat().st_mtime)
    try:
        size = newest.stat().st_size
        with open(newest, "rb") as f:
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(size - _TRANSCRIPT_TAIL_BYTES)
            data = f.read()
    except OSError:
        return None
    out: list[tuple[float, str, str]] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        etype = row.get("type")
        if not isinstance(etype, str) or not etype:
            continue
        ts = parse_entry_ts(row.get("timestamp"))
        if ts is None:
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            body = content
        elif isinstance(content, list):
            body = "\n".join(
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str))
        else:
            body = ""
        out.append((ts, etype, body))
    return out


# --- session scan + pane resolution (P10-D5/D9) --------------------------------


def collect_live_sessions() -> list[tuple[str, int, dict]]:
    """P10-D5: (key, nnn, record) for every session with state !=
    ROTATED and ``off`` false. Unreadable state.json → skip (never
    raises — P10-D12)."""
    out: list[tuple[str, int, dict]] = []
    root = paths.projects_dir()
    if not root.is_dir():
        return out
    for proj in sorted(root.iterdir()):
        sessions = proj / "sessions"
        if not proj.is_dir() or not sessions.is_dir():
            continue
        for sess in sorted(sessions.iterdir()):
            if not sess.is_dir() or not sess.name.isdigit():
                continue
            try:
                rec = json.loads(
                    (sess / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("state") == "ROTATED" or rec.get("off"):
                continue
            out.append((proj.name, int(sess.name), rec))
    return out


def resolve_pane(rec: dict, slug: str,
                 sessions: list[str] | None) -> str | None:
    """P10-D9/P10-P6: the nudge target. The hook-recorded
    ``tmux_session`` field wins WHILE IT STILL EXISTS in the live
    ``tmux list-sessions`` output; a stale field (session renamed or
    killed out-of-band — regression 2026-09-27 046, a dead ``-637d``
    pane made every nudge CAPTURE_FAILED) falls through to the prefix
    fallback. With no tmux server (``sessions is None``) existence
    cannot be verified, so the field is kept and the failed capture is
    logged CAPTURE_FAILED (the honest label). Fallback = ``sessions``
    filtered by prefix ``cc-<slug>-`` — EXACTLY one match → use it;
    zero / several / no tmux server → None (UNRESOLVABLE: log, never
    nudge)."""
    t = rec.get("tmux_session")
    if isinstance(t, str) and t.strip():
        t = t.strip()
        if sessions is None or t in sessions:
            return t
    if sessions is None:
        return None
    prefix = f"cc-{slug}-"
    matches = [s for s in sessions if s.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def last_ledger_ts(session_dir: Path | str) -> float | None:
    """P10-P9 (with the daemon-exclusion refinement): ts of the LAST
    chain-activity ledger entry. Any chain entry type counts (a
    trailing ``stop`` restarts the clock after a failed turn); the
    watchdog's own entries (``_DAEMON_TYPES`` + ``needs_human`` with
    ``source == "watchdog"``) do NOT. No ledger / no ts → None
    (treated as stale)."""
    try:
        entries = ledger.read(session_dir)
    except OSError:
        return None
    for e in reversed(entries):
        if e.get("type") in _DAEMON_TYPES:
            continue
        if e.get("type") == "needs_human" and e.get("source") == "watchdog":
            continue
        if isinstance(e.get("ts"), (int, float)):
            return float(e["ts"])
    return None


def _last_nudge_text(sdir) -> str | None:
    """Phase 19: the ``text`` of the LAST ``watchdog_nudge`` ledger entry
    (None = the daemon never nudged this session). The pending check must
    match what was SENT — every nudge's context block differs per tick
    (idle minutes, outage window), so the text freshly built for THIS
    tick would not match the text sitting in the input box."""
    try:
        entries = ledger.read(sdir)
    except OSError:
        return None
    for e in reversed(entries):
        if e.get("type") == "watchdog_nudge":
            t = e.get("text")
            return t if isinstance(t, str) and t else None
    return None


def _seed_froze() -> set:
    """P10-P7/D8: a freeze already announced before the daemon restart
    (a ``watchdog_freeze`` ledger entry not followed by a
    ``user_prompt``) is not re-pushed."""
    out = set()
    for key, nnn, _rec in collect_live_sessions():
        try:
            entries = ledger.read(paths.session_dir(key, nnn))
        except OSError:
            continue
        frozen = False
        for e in entries:
            if e.get("type") == "watchdog_freeze":
                frozen = True
            elif e.get("type") == "user_prompt":
                frozen = False
        if frozen:
            out.add((key, nnn))
    return out


# --- the tick (P10-D4/D5/D6/D7/D8) ---------------------------------------------


@dataclasses.dataclass
class TickCtx:
    """One tick's inputs (P10-P5). The I/O callables are injectable —
    tests pass fakes, ``run_forever`` passes the real wrappers.
    ``now`` is set per tick (tests pin the clock)."""
    cfg: dict
    health: HealthState
    episodes: dict      # (key, nnn) -> Episode
    prev_captures: dict  # (key, nnn) -> (capture, unchanged_ticks)
    froze: set          # (key, nnn) freeze already pushed this run
    probe: object        # () -> bool
    capture: object      # (target: str) -> str | None
    list_sessions: object  # () -> list[str] | None
    send: object         # (target: str, *keys: str) -> bool
    now: float | None = None
    recover: object = None  # () -> dict (recover_network); None = no recovery
    cycle: object = None  # () -> dict (cycle_power); None = no auto-cycle
    last_cycle_ts: object = None  # () -> float | None (cooldown state)
    record_cycle_ts: object = None  # (ts: float) -> None
    sleep: object = None  # (s: float) -> None (settle pauses; tests pin)
    # Phase 21: the exit-hang fast pass (check_exit_hangs). None = the
    # fast pass is disabled. exit_hang_killed is the daemon-lifetime
    # re-kill/re-push guard: a zombie keeps kill(pid, 0) succeeding, so
    # a killed pid is skipped for the rest of the daemon's life.
    list_panes: object = None  # (target: str) -> list[dict] | None
    ps_snapshot: object = None  # () -> dict | None (the process map)
    read_transcript: object = None  # (cwd: str) -> list | None
    is_alive: object = None  # (pid: int) -> bool
    kill: object = None  # (pid: int) -> str ("term"/"kill"/"dead")
    exit_hang_killed: set = dataclasses.field(default_factory=set)


def _verify(ctx: TickCtx, key: str, nnn: int, slug: str,
            line: dict) -> bool:
    """P10-D7.2/P10-P4: a ``user_prompt`` with ``ts > nudge_ts`` = the
    nudge landed (the ledger entry is the evidence — the 11:30
    lesson). Pushes ``turn_recovered``, records ``watchdog_verified``,
    resets the episode (also clears ``given_up`` — a human prompt ends
    the episode). Runs for every open episode, BEFORE classification —
    a landing nudge makes the ledger fresh (ALIVE), so the check must
    not live in the TURN_DEAD/STALLED branch (see Task 7 plan note)."""
    ep = ctx.episodes.get((key, nnn))
    if ep is None or ep.nudge_ts is None:
        return False
    sdir = paths.session_dir(key, nnn)
    try:
        entries = ledger.read(sdir)
    except OSError:
        return False
    for e in reversed(entries):
        if (e.get("type") == "user_prompt"
                and isinstance(e.get("ts"), (int, float))
                and e["ts"] > ep.nudge_ts):
            _push(ctx.cfg, "turn_recovered", slug=slug, nnn=nnn,
                  nudge=ep.n)
            ledger.append(sdir, {"type": "watchdog_verified",
                                 "n": ep.n})
            ctx.episodes.pop((key, nnn), None)
            line["events"].append(f"recovered:{key}:{nnn:03d}")
            return True
    return False


def _restate(key: str, nnn: int) -> dict | None:
    """State re-read at send time (P10-D5 rotation race). Never raises."""
    try:
        return state.load_state(key, nnn)
    except (state.StateNotFoundError, OSError):
        return None


def _focus_session() -> tuple[str, int] | None:
    """The one most-recently-active NUDGE_ELIGIBLE session (max
    ``last_ledger_ts``), as ``(key, nnn)`` or None. Mirrors the digest
    relay's target definition (``watchdog_digest._best_candidate``): the
    nudge restriction matches the digest target so only the session the
    digest talks to may be revived. Used to gate nudges when
    ``watchdog_nudge_only_most_recent`` is on — at most one session (the most
    recent) may make progress per tick, capping the shared DGX model's load.
    Never raises (P10-D12)."""
    best = None  # (last_ts, key, nnn)
    for key, nnn, rec in collect_live_sessions():
        if rec.get("state") not in NUDGE_ELIGIBLE:
            continue
        ts = last_ledger_ts(paths.session_dir(key, nnn))
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if best is None or ts > best[0]:
            best = (ts, key, nnn)
    return None if best is None else (best[1], best[2])


def _may_nudge(ctx: TickCtx, focus: tuple[str, int] | None,
               key: str, nnn: int) -> bool:
    """Whether this session may receive a nudge this tick. Config off
    (default) -> every eligible session may (legacy behavior). Config on ->
    only the focus session (the most-recently-active eligible one, matching
    the digest target) may; the others stay dormant and do not burn nudges."""
    if not bool(ctx.cfg.get("watchdog_nudge_only_most_recent", False)):
        return True
    return (key, nnn) == focus


def _nudge_path(ctx: TickCtx, key: str, nnn: int, target: str, cap: str,
                last_ts: float | None, now: float, line: dict, *,
                outage: float | None = None,
                cycle_ts: float | None = None,
                stalled: bool = False,
                sent_text: str | None = None) -> None:
    """P10-D7 nudge state machine, executed with side effects (the
    daemon owns them — the core stays pure, P10-P1).

    Phase 19: ``outage``/``cycle_ts`` build the OUTAGE-form nudge text
    (dgx_up recovery — the model is back, the turn died with it);
    otherwise the plain form (``stalled`` picks the STALLED line).
    ``sent_text``: the text of the LAST nudge the daemon actually sent
    (``_last_nudge_text``) — the pending check must match THAT, because
    the freshly built text's context block differs per tick. On the
    first physical send (action NUDGE) a force-cancel ``Escape`` +
    settle precede the text: it kills the stuck in-flight turn and a
    stale duplicate line left in the input box (RESEND/SEND_ENTER must
    NOT ESC — that would kill the pending nudge it is submitting)."""
    cfg = ctx.cfg
    sid = (key, nnn)
    sdir = paths.session_dir(key, nnn)
    max_nudges = int(cfg.get("watchdog_max_nudges", 3))
    base = str(cfg.get("watchdog_nudge_text") or "continue")
    slug = notify.project_slug(key)
    idle_minutes = (int((now - last_ts) // 60)
                    if last_ts is not None
                    else int(cfg.get("watchdog_stale_minutes", 45)))
    if outage is not None:
        text = build_nudge_text(base, now=now, down_since=outage,
                                cycle_ts=cycle_ts)
    else:
        text = build_nudge_text(base, now=now, idle_minutes=idle_minutes,
                                stalled=stalled)
    pending = pane_pending(cap, text) or (
        sent_text is not None and pane_pending(cap, sent_text))

    ep = ctx.episodes.get(sid)
    if ep is None:
        ep = ctx.episodes[sid] = Episode(
            waiting_health=ctx.health.status != "UP")

    action = nudge_action(ctx.health.status, ep, pending, max_nudges)
    if action == "WAIT_HEALTH":
        line["events"].append(f"waiting_for_dgx:{key}:{nnn:03d}")
        return
    if action == "DONE":
        return  # given_up: waiting for a human user_prompt

    if action in ("NUDGE", "RESEND"):
        # rotation race (P10-D5): state re-read at send time
        fresh = _restate(key, nnn)
        if fresh is None or fresh.get("state") not in NUDGE_ELIGIBLE:
            line["events"].append(f"skip_state:{key}:{nnn:03d}")
            return
        if action == "NUDGE":
            # Phase 19 item 2: force-cancel the stuck in-flight turn
            # first, let it settle, THEN the enriched text lands.
            try:
                ctx.send(target, "Escape")
            except Exception:
                _log_error("send")
            (ctx.sleep or time.sleep)(ESCAPE_SETTLE_S)
        try:
            ok = ctx.send(target, text, "Enter")
        except Exception:
            _log_error("send")
            ok = False
        if not ok:
            line["events"].append(f"send_failed:{key}:{nnn:03d}")
            return
        ledger.append(sdir, {"type": "watchdog_nudge", "n": ep.n + 1,
                             "text": text})
        ep.n += 1
        ep.nudge_ts = now
        if action == "NUDGE":
            _push(cfg, "turn_dead", slug=slug, nnn=nnn,
                  idle_minutes=idle_minutes, max_nudges=max_nudges)
            line["events"].append(f"turn_dead:{key}:{nnn:03d}")
        # RESEND: ledger-only (P10-P7) — no push
        line["events"].append(f"nudge:{key}:{nnn:03d}")
    elif action == "SEND_ENTER":
        try:
            ok = ctx.send(target, "Enter")
        except Exception:
            _log_error("send")
            ok = False
        if ok:
            line["events"].append(f"send_enter:{key}:{nnn:03d}")
    elif action == "GIVE_UP":
        reason = (f"watchdog: {max_nudges} nudges unverified "
                  "(turn dead?)")
        _push(cfg, "needs_human", slug=slug, nnn=nnn, reason=reason,
              host=notify.host())
        ledger.append(sdir, {"type": "needs_human", "reason": reason,
                             "source": "watchdog"})
        ep.given_up = True
        line["events"].append(f"give_up:{key}:{nnn:03d}")


def _recover_stuck_turns(ctx: TickCtx, sessions, line: dict,
                         focus: tuple[str, int] | None,
                         down_since: float | None = None) -> None:
    """P15: after a DOWN->UP transition, nudge each eligible session still
    mid-turn (turn_active). A real DOWN (2-fail debounce) means the model was
    down, so no turn could progress — a session still mid-turn is stuck on the
    outage's API error. classify() keeps calling it ALIVE (last_ts fresh <
    stale minutes), so the normal nudge path never sees it; the model is back,
    so a nudge now resumes it. Reuses _nudge_path (health is now UP) so the
    episode/ledger/push/verification machinery stays consistent. Runs only on
    the transition tick (the caller gates it) so a recovery turn in flight is
    not re-nudged next tick. When ``watchdog_nudge_only_most_recent`` is on,
    ``focus`` (the most-recently-active eligible session, the digest target)
    is the only one recovered this tick.

    Phase 19: the nudge carries the OUTAGE context (down_since + the
    power_cycle ts when it falls inside the window) so the session knows
    the turn died WITH the model and what to check first. Never raises
    (P10-D12)."""
    now = ctx.now if ctx.now is not None else time.time()
    stale_s = float(ctx.cfg.get("watchdog_stale_minutes", 45)) * 60
    # The power_cycle that caused this outage (if any): only a cycle ts
    # INSIDE the outage window belongs in the nudge text.
    cycle_ts = None
    if down_since is not None and ctx.last_cycle_ts is not None:
        try:
            cts = ctx.last_cycle_ts()
        except Exception:
            _log_error("last_cycle_ts")
            cts = None
        if cts is not None and down_since <= cts <= now:
            cycle_ts = cts
    for key, nnn, rec in collect_live_sessions():
        if rec.get("state") not in NUDGE_ELIGIBLE:
            continue
        if not rec.get("turn_active"):
            continue  # not mid-turn -> not stuck on the outage
        last_ts = last_ledger_ts(paths.session_dir(key, nnn))
        # classify() masks a FRESH-ledger stuck turn as ALIVE (row 1) — that is
        # exactly what the per-session loop will NOT nudge, so we own it here.
        # A stale ledger classifies TURN_DEAD/STALLED and the loop's normal
        # nudge path already resumes it on this same tick — skip it so we do
        # not double-nudge.
        if last_ts is None or now - last_ts > stale_s:
            continue
        if not _may_nudge(ctx, focus, key, nnn):
            continue  # not the focus session -> stay dormant (config on)
        slug = notify.project_slug(key)
        target = resolve_pane(rec, slug, sessions)
        if target is None:
            line["events"].append(f"recovery_unresolvable:{key}:{nnn:03d}")
            continue
        try:
            cap = ctx.capture(target)
        except Exception:
            _log_error("recovery_capture")
            cap = None
        if cap is None:
            line["events"].append(f"recovery_capture_failed:{key}:{nnn:03d}")
            continue
        _nudge_path(ctx, key, nnn, target, cap, last_ts, now, line,
                    outage=down_since, cycle_ts=cycle_ts)
        line["events"].append(f"dgx_up_recovery:{key}:{nnn:03d}")


def tick(ctx: TickCtx) -> dict:
    """One watchdog tick: probe → health transitions → per-live-session
    (verify → resolve → capture → classify → act). Returns the tick's
    JSONL log line; notable events log themselves. I/O boundaries are
    wrapped (P10-D12) — a failure degrades, it does not raise."""
    now = ctx.now if ctx.now is not None else time.time()
    line = {"ts": now, "health": ctx.health.status,
            "sessions": [], "events": []}

    # 1. health probe + transitions (P10-D4; a server restart is
    #    detected within 2×interval — probe per tick, 2-fail debounce)
    try:
        ok = bool(ctx.probe())
    except Exception:
        _log_error("probe")
        ok = False

    # network recovery (P10): a failed probe may be a stale route (DGX/Mac
    # restart), not a real outage — run the configured macOS wrapper, log
    # its full output, re-probe, and only then count this tick as a fail.
    recovery = None
    if not ok and ctx.recover is not None:
        try:
            recovery = ctx.recover()
        except Exception:
            _log_error("recover")
        if recovery and recovery.get("ran"):
            _recovery_log(ctx.cfg, recovery)
            try:
                ok = bool(ctx.probe())  # second chance after recovery
            except Exception:
                _log_error("probe")
                ok = False
    if recovery is not None:
        line["recovery"] = {"ran": recovery.get("ran"),
                            "exit": recovery.get("exit"),
                            "elapsed_s": round(
                                float(recovery.get("elapsed_s") or 0.0), 3)}
    down_since = ctx.health.down_since
    transition = ctx.health.feed(ok, now)
    if transition == "down":
        _push(ctx.cfg, "dgx_down", slug="", nnn=0, host=notify.host(),
              interval_min=round(
                  int(ctx.cfg.get("watchdog_interval_s", 300)) / 60))
        line["events"].append("dgx_down")
    elif transition == "up":
        _push(ctx.cfg, "dgx_up", slug="", nnn=0,
              downtime_s=(now - down_since) if down_since else 0.0)
        line["events"].append("dgx_up")

    # 1b. auto power-cycle (Phase 13, design 7899): sustained DOWN past
    #     power_cycle_after_s + cooldown clear + plug configured ->
    #     POST the localhost endpoint /cycle. A FAILED attempt does not
    #     consume the cooldown (broken plug/endpoint): it retries next
    #     tick; only a real relay cycle arms the 30-min window.
    if (ctx.cycle is not None and ctx.health.status == "DOWN"
            and str(ctx.cfg.get("power_plug_ip") or "").strip()):
        try:
            last = ctx.last_cycle_ts() if ctx.last_cycle_ts is not None \
                else None
        except Exception:
            _log_error("last_cycle_ts")
            last = None
        if power_cycle_due(ctx.health.status, ctx.health.down_since, now,
                           last,
                           float(ctx.cfg.get("power_cycle_after_s", 600)),
                           float(ctx.cfg.get("power_cycle_cooldown_s",
                                          1800))):
            try:
                res = ctx.cycle()
            except Exception:
                _log_error("cycle")
                res = None
            res = res if isinstance(res, dict) else {}
            downtime_s = now - (ctx.health.down_since or now)
            line["power_cycle"] = {"ok": bool(res.get("ok")),
                                   "state": res.get("state", "unknown"),
                                   "downtime_s": downtime_s}
            if res.get("ok"):
                if ctx.record_cycle_ts is not None:
                    try:
                        ctx.record_cycle_ts(now)
                    except Exception:
                        _log_error("record_cycle")
                _push(ctx.cfg, "dgx_power_cycle", slug="", nnn=0,
                      host=notify.host(), downtime_s=downtime_s)
                line["events"].append("power_cycle")
            else:
                line["events"].append(
                    "power_cycle_failed:"
                    + str(res.get("error", "unknown"))[:60])

    # 2. live sessions (P10-D5)
    try:
        sessions = ctx.list_sessions()
    except Exception:
        _log_error("list_sessions")
        sessions = None
    if sessions is None:
        line["events"].append("tmux_unavailable")

    # 2a. nudge focus (watchdog_nudge_only_most_recent): when the config is
    #     on, at most one session may be nudged this tick — the
    #     most-recently-active eligible one (the digest target). Computed once
    #     here and passed to both nudge sites below. None when off (legacy:
    #     every eligible session may be nudged).
    focus = (_focus_session()
             if bool(ctx.cfg.get("watchdog_nudge_only_most_recent", False))
             else None)

    # 2b. outage recovery (Phase 15): the model just came back (transition
    #     "up"). Nudge each eligible session still mid-turn (turn_active) — its
    #     turn died on the outage's API error and classify() masks it as ALIVE
    #     (fresh last_ts). Only on the transition tick, so a recovery turn in
    #     flight is not re-nudged on a later UP tick.
    if transition == "up":
        _recover_stuck_turns(ctx, sessions, line, focus, down_since)

    for key, nnn, rec in collect_live_sessions():
        sid = (key, nnn)
        entry = {"key": key, "nnn": nnn, "outcome": None}
        line["sessions"].append(entry)
        slug = notify.project_slug(key)

        # verification FIRST (binding placement — see _verify docstring)
        if _verify(ctx, key, nnn, slug, line):
            entry["outcome"] = "VERIFIED"
            continue

        target = resolve_pane(rec, slug, sessions)
        if target is None:
            entry["outcome"] = "UNRESOLVABLE"
            line["events"].append(f"unresolvable:{key}:{nnn:03d}")
            continue
        try:
            cap = ctx.capture(target)
        except Exception:
            _log_error("capture")
            cap = None
        if cap is None:
            entry["outcome"] = "CAPTURE_FAILED"
            line["events"].append(f"capture_failed:{key}:{nnn:03d}")
            continue

        # unchanged-tick counter (P10-P3)
        prev = ctx.prev_captures.get(sid)
        if prev is None:
            ticks = 0
        else:
            ticks = prev[1] + 1 if cap == prev[0] else 0
        ctx.prev_captures[sid] = (cap, ticks)

        sdir = paths.session_dir(key, nnn)
        last_ts = last_ledger_ts(sdir)
        sent_text = _last_nudge_text(sdir)
        stale_s = float(ctx.cfg.get("watchdog_stale_minutes", 45)) * 60
        outcome = classify(rec, last_ts, cap,
                           prev[0] if prev is not None else None,
                           now, stale_s, ticks)
        entry["outcome"] = outcome
        if outcome != "ALIVE":
            tail = _last_nonempty(cap)
            if tail:
                entry["tail"] = tail[:200]

        if outcome in ("TURN_DEAD", "STALLED"):
            if _may_nudge(ctx, focus, key, nnn):
                _nudge_path(ctx, key, nnn, target, cap, last_ts, now, line,
                            stalled=(outcome == "STALLED"),
                            sent_text=sent_text)
        elif (outcome in ("WAIT", "FROZEN") and sent_text is not None
              and pane_pending(cap, sent_text)
              and _may_nudge(ctx, focus, key, nnn)):
            # Phase 19: a pending MULTI-LINE nudge makes classify() see a
            # non-idle pane (the ❯ glyph is on the FIRST input line only)
            # → WAIT/FROZEN → the normal path would never submit it (it
            # rots in the input box). Submit it with a bare Enter.
            _nudge_path(ctx, key, nnn, target, cap, last_ts, now, line,
                        sent_text=sent_text)
        elif outcome == "FROZEN" and sid not in ctx.froze:
            _push(ctx.cfg, "freeze_detected", slug=slug, nnn=nnn)
            ledger.append(sdir, {"type": "watchdog_freeze"})
            ctx.froze.add(sid)
            line["events"].append(f"freeze:{key}:{nnn:03d}")
    return line


def check_exit_hangs(ctx: TickCtx) -> list[dict]:
    """Phase 21: the 5-second fast pass (P10-D12: every I/O wrapped, a
    failure degrades — it never raises into the loop).

    Sequence (pinned by tests): live tmux sessions → live records →
    pane target (the HIGHEST nnn wins per target — an in-place
    rotation shares the pane) → the turn guard BEFORE the ps scan
    (all mid-turn means zero ps) → ONE process map for the whole pass
    → per pane: topmost claude → liveness → transcript marker + grace
    + activity rule → kill. A ``"dead"`` kill lost the race: the pid
    still joins the killed-set (the zombie guard) but gets NO push and
    NO log. A no-op pass writes ZERO log lines (the 5s cadence must not
    bloat the log). Returns the actions taken (one dict per kill)."""
    try:
        sessions = ctx.list_sessions()
    except Exception:
        _log_error("exit_hang list_sessions")
        return []
    if sessions is None:
        return []
    # live records → target; per target the highest nnn wins
    by_target: dict[str, tuple[str, int, dict, str]] = {}
    for key, nnn, rec in collect_live_sessions():
        slug = notify.project_slug(key)
        target = resolve_pane(rec, slug, sessions)
        if target is None:
            continue
        prev = by_target.get(target)
        if prev is None or nnn > prev[1]:
            by_target[target] = (key, nnn, rec, slug)
    # the turn guard BEFORE the ps scan: a mid-turn /exit waits for the
    # Stop hook to clear turn_active — all mid-turn means no ps at all
    eligible = [(target, key, nnn, slug)
                for target, (key, nnn, rec, slug) in by_target.items()
                if not rec.get("turn_active")]
    if not eligible:
        return []
    try:
        procmap = ctx.ps_snapshot()
    except Exception:
        _log_error("exit_hang ps_snapshot")
        return []
    if not procmap:
        return []
    now = ctx.now if ctx.now is not None else time.time()
    limit_s = exit_hang_limit_s()
    actions: list[dict] = []
    for target, key, nnn, slug in eligible:
        try:
            panes = ctx.list_panes(target)
        except Exception:
            _log_error("exit_hang list_panes")
            continue
        if not panes:
            continue
        for pane in panes:
            pid = find_claude_pid(pane["pid"], procmap)
            if pid is None or pid in ctx.exit_hang_killed:
                continue
            try:
                alive = bool(ctx.is_alive(pid))
                entries = ctx.read_transcript(pane["path"])
            except Exception:
                _log_error("exit_hang transcript")
                continue
            if not alive:
                continue
            if entries is None or not exit_hang_due(entries, now, limit_s):
                continue
            age_s = now - find_exit_marker_ts(entries)
            try:
                mode = ctx.kill(pid)
            except Exception:
                _log_error("exit_hang kill")
                continue
            ctx.exit_hang_killed.add(pid)
            if mode == "dead":
                continue  # lost the race: no push, NO log
            _push(ctx.cfg, "exit_hang", slug=slug, nnn=nnn,
                  age_s=age_s, mode=mode)
            action = {"key": key, "nnn": nnn, "pane": target, "pid": pid,
                      "mode": mode, "age_s": age_s}
            _log_line({"ts": now, "events": [f"exit_hang_killed:{key}"],
                       **action})
            actions.append(action)
    return actions


def run_forever(cfg: dict, *, max_ticks: int | None = None,
                sleep=time.sleep, now=time.time, probe=None,
                capture=None, list_sessions=None, send=None,
                recover=None, cycle=None, last_cycle_ts=None,
                record_cycle_ts=None, list_panes=None,
                ps_snapshot=None, read_transcript=None, kill=None,
                is_alive=None) -> int:
    """The watchdog loop (P10-D2 ``watchdog run`` — the plist AND the
    integration tests use it). ``watchdog_enabled: false`` → return 0
    immediately (after config load, before the loop — no tick, no I/O).
    Any fast-pass or tick exception → one errors.log line + continue
    (P10-D12).

    Phase 21 — one loop, two cadences: EVERY iteration runs the
    exit-hang fast pass (``EXIT_HANG_POLL_S`` = 5s); the full tick
    (probe + session scan) only when ``watchdog_interval_s`` has
    elapsed. The first iteration ticks immediately (the old
    every-iteration-was-a-tick startup behavior). ``max_ticks`` counts
    full ticks."""
    if not cfg.get("watchdog_enabled"):
        return 0
    interval = float(cfg.get("watchdog_interval_s", 300))
    ctx = TickCtx(
        cfg=cfg,
        health=HealthState(),
        episodes={},
        prev_captures={},
        froze=_seed_froze(),
        probe=probe if probe is not None else (lambda: probe_health(cfg)),
        capture=capture if capture is not None else capture_pane,
        list_sessions=list_sessions if list_sessions is not None
        else list_tmux_sessions,
        send=send if send is not None else send_keys,
        recover=(recover if recover is not None
                 else (lambda: recover_network(cfg))),
        cycle=(cycle if cycle is not None
               else (lambda: cycle_power(cfg))),
        last_cycle_ts=(last_cycle_ts if last_cycle_ts is not None
                       else load_last_cycle_ts),
        record_cycle_ts=(record_cycle_ts if record_cycle_ts is not None
                         else save_last_cycle_ts),
        list_panes=list_panes if list_panes is not None else list_tmux_panes,
        ps_snapshot=ps_snapshot if ps_snapshot is not None else ps_tree,
        read_transcript=(read_transcript if read_transcript is not None
                         else read_transcript_entries),
        is_alive=is_alive if is_alive is not None else pid_alive,
        kill=kill if kill is not None else kill_process,
    )
    ticks = 0
    last_tick = None
    while True:
        ctx.now = now()
        try:
            check_exit_hangs(ctx)
        except Exception:
            _log_error("exit_hang")
        if last_tick is None or ctx.now - last_tick >= interval:
            last_tick = ctx.now
            try:
                line = tick(ctx)
            except Exception:
                _log_error("tick")
                line = {"ts": ctx.now, "error": "tick exception"}
            _log_line(line)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return 0
        sleep(EXIT_HANG_POLL_S)


# --- plist / launchctl (P10-D2/P10-P8) -----------------------------------------


def plist_path(home: Path | str) -> Path:
    return (Path(home) / "Library" / "LaunchAgents"
            / f"{PLIST_LABEL}.plist")


def plist_render(binary: str, home: Path | str,
                 tmux_bin: str | None = None) -> str:
    """P10-P8: the LaunchAgent plist. The absolute ``cc-continuity``
    path is baked at install time (doctor re-verifies it — R5); so is the
    absolute tmux path (``CC_CONTINUITY_TMUX_BIN``) — launchd's minimal
    PATH has no homebrew, so a bare ``tmux`` never resolves there and the
    daemon logged ``tmux_unavailable`` on every tick."""
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
        "    <string>watchdog</string>\n    <string>run</string>\n"
        "  </array>\n"
        f"{tmux_env}"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{home}</string>\n"
        f"  <key>StandardOutPath</key>\n"
        f"  <string>{logs}/watchdog.out</string>\n"
        f"  <key>StandardErrorPath</key>\n"
        f"  <string>{logs}/watchdog.err</string>\n"
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
    """P10-D2: resolve the installed binary, write the plist,
    ``launchctl bootstrap gui/<uid> <plist>``. Raises WatchdogError on
    failure (CLI-level). ``which`` defaults to ``shutil.which`` resolved
    AT CALL TIME (a def-time default would defeat the tests'
    ``monkeypatch.setattr(watchdogd.shutil, "which", ...)``)."""
    if which is None:
        which = shutil.which
    binary = which("cc-continuity")
    if binary is None:
        raise WatchdogError("cc-continuity not on PATH (install first)")
    p = plist_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plist_render(binary, Path(home),
                              tmux_bin=which("tmux")), encoding="utf-8")
    if not _launchctl(runner, "bootstrap", f"gui/{os.getuid()}", str(p)):
        raise WatchdogError(
            "launchctl bootstrap failed — see `cc-continuity doctor`")
    return p


def uninstall_plist(home: Path | str, *, runner=subprocess.run) -> None:
    _launchctl(runner, "bootout", f"gui/{os.getuid()}/{PLIST_LABEL}")
    plist_path(home).unlink(missing_ok=True)


def start_plist(home: Path | str, *, runner=subprocess.run) -> bool:
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
