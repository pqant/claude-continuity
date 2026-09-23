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
import subprocess
import time
import traceback
from pathlib import Path

from . import ledger, notify, paths, state
from .watchdog import (Episode, HealthState, classify, nudge_action,
                       pane_pending)

#: P10-D2/P10-P8: the LaunchAgent label (plist file name minus .plist).
PLIST_LABEL = "com.claude-continuity.watchdog"

#: States whose sessions may receive a nudge (P10-D5).
NUDGE_ELIGIBLE = frozenset({"ACTIVE", "WARNED"})

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
    ``tmux_session`` field wins; fallback = ``tmux list-sessions``
    filtered by prefix ``cc-<slug>-`` — EXACTLY one match → use it;
    zero / several / no tmux server → None (UNRESOLVABLE: log, never
    nudge)."""
    t = rec.get("tmux_session")
    if isinstance(t, str) and t.strip():
        return t.strip()
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


def _nudge_path(ctx: TickCtx, key: str, nnn: int, target: str, cap: str,
                last_ts: float | None, now: float, line: dict) -> None:
    """P10-D7 nudge state machine, executed with side effects (the
    daemon owns them — the core stays pure, P10-P1)."""
    cfg = ctx.cfg
    sid = (key, nnn)
    sdir = paths.session_dir(key, nnn)
    max_nudges = int(cfg.get("watchdog_max_nudges", 3))
    text = str(cfg.get("watchdog_nudge_text") or "continue")
    slug = notify.project_slug(key)

    ep = ctx.episodes.get(sid)
    if ep is None:
        ep = ctx.episodes[sid] = Episode(
            waiting_health=ctx.health.status != "UP")

    action = nudge_action(ctx.health.status, ep,
                          pane_pending(cap, text), max_nudges)
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
            idle_minutes = (int((now - last_ts) // 60)
                            if last_ts is not None
                            else int(cfg.get("watchdog_stale_minutes", 45)))
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

    # 2. live sessions (P10-D5)
    try:
        sessions = ctx.list_sessions()
    except Exception:
        _log_error("list_sessions")
        sessions = None
    if sessions is None:
        line["events"].append("tmux_unavailable")

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
            _nudge_path(ctx, key, nnn, target, cap, last_ts, now, line)
        elif outcome == "FROZEN" and sid not in ctx.froze:
            _push(ctx.cfg, "freeze_detected", slug=slug, nnn=nnn)
            ledger.append(sdir, {"type": "watchdog_freeze"})
            ctx.froze.add(sid)
            line["events"].append(f"freeze:{key}:{nnn:03d}")
    return line


def run_forever(cfg: dict, *, max_ticks: int | None = None,
                sleep=time.sleep, probe=None, capture=None,
                list_sessions=None, send=None) -> int:
    """The watchdog loop (P10-D2 ``watchdog run`` — the plist AND the
    integration tests use it). ``watchdog_enabled: false`` → return 0
    immediately (after config load, before the loop — no tick, no I/O).
    Any tick exception → one errors.log line + continue (P10-D12)."""
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
    )
    ticks = 0
    while True:
        ctx.now = time.time()
        try:
            line = tick(ctx)
        except Exception:
            _log_error("tick")
            line = {"ts": ctx.now, "error": "tick exception"}
        _log_line(line)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return 0
        sleep(interval)


# --- plist / launchctl (P10-D2/P10-P8) -----------------------------------------


def plist_path(home: Path | str) -> Path:
    return (Path(home) / "Library" / "LaunchAgents"
            / f"{PLIST_LABEL}.plist")


def plist_render(binary: str, home: Path | str) -> str:
    """P10-P8: the LaunchAgent plist. The absolute ``cc-continuity``
    path is baked at install time (doctor re-verifies it — R5)."""
    logs = paths.logs_dir()
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
    p.write_text(plist_render(binary, Path(home)), encoding="utf-8")
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
