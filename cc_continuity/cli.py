"""cc-continuity command-line entry point (PRD §16).

Phase 2 implements ``statusline`` (PRD §7) and ``status [--json]``;
Phase 3 implements ``hook <event>`` (PRD §9) and ``history``.
Phase 4 implements ``checkpoint`` / ``validate`` / ``validate-draft``
(PRD §10/§12); Phase 6 implements ``rotate`` (PRD §11).
Phase 7 implements ``install`` / ``uninstall`` / ``doctor`` (PRD §17)
and ``launch`` (the zsh wrapper backend, Appendix D).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import (__version__, blink_watch, bootstrap, checkpoint, config,
               ledger, naming, paths, project, progress_pane, state,
               statusline, thresholds, validate)
from .hooks import (common, pre_compact, pre_tool_use, session_end,
                    session_start, stop, task, tool_use, user_prompt)

# event -> handler (PRD §9)
HOOK_HANDLERS: dict[str, Callable[[common.HookContext], dict | None]] = {
    "SessionStart": session_start.handle,
    "UserPromptSubmit": user_prompt.handle,
    "PreToolUse": pre_tool_use.handle,
    "PostToolUse": tool_use.handle,
    "PostToolUseFailure": tool_use.handle_failure,
    "TaskCreated": task.handle,
    "TaskCompleted": task.handle,
    "Stop": stop.handle,
    "PreCompact": pre_compact.handle,
    "SessionEnd": session_end.handle,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-continuity",
        description="Zero-touch session rotation for Claude Code.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_status = sub.add_parser("status",
                              help="current project/session summary")
    p_status.add_argument("--json", action="store_true",
                          help="machine-readable output")
    p_status.add_argument("slug", nargs="?",
                          help="project slug (default: current project)")
    sub.add_parser("statusline", help="status line entry (PRD §7)")

    p_hook = sub.add_parser("hook", help="hook entry (PRD §9)")
    p_hook.add_argument("event",
                        help="hook event name (e.g. UserPromptSubmit)")

    p_hist = sub.add_parser("history", help="session chain for this project")
    p_hist.add_argument("-n", type=int, default=20,
                        help="show at most N most recent sessions (default 20)")

    p_ratio = sub.add_parser("ratio",
                             help="print the live context ratio of a project")
    p_ratio.add_argument("slug", nargs="?",
                         help="project slug (default: current project)")
    p_ratio.add_argument("--json", action="store_true",
                         help="machine-readable output")

    p_ledger = sub.add_parser("ledger",
                              help="print recent ledger entries of the "
                                   "current session (issue #46)")
    p_ledger.add_argument("n", nargs="?", type=int, default=20,
                          help="how many newest entries (default 20)")

    p_vd = sub.add_parser("validate-draft",
                          help="validate a handoff draft (PRD §10.1)")
    p_vd.add_argument("file", help="path to handoff.draft.md")

    p_val = sub.add_parser("validate", help="validate a checkpoint (PRD §12)")
    p_val.add_argument("--checkpoint", required=True, dest="checkpoint",
                       help="checkpoint directory to validate")

    p_cp = sub.add_parser("checkpoint",
                          help="build + finalize a checkpoint now (PRD §10)")
    p_cp.add_argument("--no-rotate", action="store_true",
                      help="finalize without rotating (rotation lands in "
                           "Phase 6; no-op in Phase 4)")

    sub.add_parser("request-rotation",
                   help="move the current session to ROTATION_PENDING now")
    sub.add_parser("off",
                   help="disable cc-continuity for the current session")
    sub.add_parser("on",
                   help="re-enable cc-continuity for the current session")

    p_rep = sub.add_parser("repair",
                           help="admin repair of stuck/ghost ledger records")
    rep_sub = p_rep.add_subparsers(dest="repair_command", metavar="<target>")
    p_rep_s = rep_sub.add_parser(
        "sessions", help="close stuck records / mark ghosts off (admin)")
    p_rep_s.add_argument("--project", required=True, dest="project_arg",
                         help="project key (dir under ~/.claude-continuity/"
                              "projects) or repo path")
    p_rep_s.add_argument("--ghost", default="",
                         help="comma-separated nnn list: mark records that "
                              "never had a conversation off")
    p_rep_s.add_argument("--close", default="",
                         help="comma-separated nnn list: force-close "
                              "non-terminal records whose process is gone")
    p_rep_s.add_argument("--reason", default="admin repair",
                         help="ledger reason recorded on closed records")

    p_rot = sub.add_parser("rotate", help="rotator (PRD §11)")
    p_rot.add_argument("--session", required=True,
                       help="Claude Code session id being rotated")
    p_rot.add_argument("--pane", required=True,
                       help="tmux pane target (TMUX_PANE)")
    p_rot.add_argument("--rotation-id", required=True, dest="rotation_id",
                       help="rotation id written by the Stop hook")

    p_rr = sub.add_parser(
        "rotate-retry",
        help="self-retry tick for a deferred rotation (issue #53)")
    p_rr.add_argument("--key", required=True,
                      help="project key written by the rotator")
    p_rr.add_argument("--nnn", required=True, type=int,
                      help="deferred session number")
    p_rr.add_argument("--pane", required=True,
                      help="tmux pane target (TMUX_PANE)")
    p_rr.add_argument("--session", required=True,
                      help="Claude Code session id being rotated")
    p_rr.add_argument("--rotation-id", required=True, dest="rotation_id",
                      help="rotation id of the deferred rotation")
    p_rr.add_argument("--attempt", type=int, default=1,
                      help="self-retry attempt number")

    p_inst = sub.add_parser("install",
                            help="install cc-continuity (PRD §17)")
    p_inst.add_argument("--window", type=int, default=None,
                        help="context window tokens "
                             "(default: keep current / 262144)")
    p_inst.add_argument("--ntfy", default=None,
                        help="ntfy endpoint URL (or 'off' to disable; "
                             "prompts on a TTY when omitted)")
    p_un = sub.add_parser("uninstall",
                          help="remove cc-continuity (PRD §17)")
    p_un.add_argument("--purge", action="store_true",
                      help="also delete ~/.claude-continuity (incl. projects)")
    p_doc = sub.add_parser("doctor", help="health checks (PRD §16)")
    p_doc.add_argument("--json", action="store_true",
                       help="machine-readable output")

    p_cfg = sub.add_parser("configure",
                           help="update the config after install (Phase 9)")
    p_cfg.add_argument("--ntfy", default=None,
                       help="ntfy endpoint URL (or 'off' to disable)")
    p_cfg.add_argument("--unattended", action="store_true",
                       help="set config.json unattended=true (loud: the "
                            "agent then runs without per-action approval — "
                            "see README)")
    p_cfg.add_argument("--watchdog-health", default=None,
                       help="model server health URL (or 'off' = "
                            "liveness-only mode)")
    p_cfg.add_argument("--watchdog-enabled", default=None,
                       choices=["true", "false"],
                       help="enable/disable the watchdog daemon")
    p_cfg.add_argument("--show", action="store_true",
                       help="print the effective config (read-only) and exit")
    p_cfg.add_argument("--json", action="store_true",
                       help="with --show: one JSON object for scripts")

    p_wd = sub.add_parser("watchdog",
                          help="LLM watchdog daemon (Phase 10)")
    wd_sub = p_wd.add_subparsers(dest="watchdog_command", metavar="<action>")
    wd_sub.add_parser("install", help="install the launchd LaunchAgent")
    wd_sub.add_parser("uninstall", help="remove the LaunchAgent")
    wd_sub.add_parser("start", help="kickstart the running LaunchAgent")
    wd_sub.add_parser("stop", help="bootout the LaunchAgent")
    wd_sub.add_parser("status", help="show the LaunchAgent status")
    wd_sub.add_parser("run", help="run the watchdog loop (foreground)")
    p_wd_dig = wd_sub.add_parser(
        "digest", help="watchdog log digest relay (Phase 13)")
    dig_sub = p_wd_dig.add_subparsers(
        dest="digest_command", metavar="<action>")
    dig_sub.add_parser(
        "run", help="run one digest pass (foreground)")
    dig_sub.add_parser(
        "run-loop",
        help="run the periodic digest loop in the foreground (tmux host)")
    dig_sub.add_parser(
        "install", help="install the launchd periodic agent")
    dig_sub.add_parser("uninstall", help="remove the LaunchAgent")
    dig_sub.add_parser("start", help="kickstart (run one pass now)")
    dig_sub.add_parser("stop", help="bootout the LaunchAgent")
    dig_sub.add_parser("status", help="show the LaunchAgent status")

    p_d = sub.add_parser(
        "daemon",
        help="tmux-hosted daemons (WSL/Linux; macOS uses launchd)")
    d_sub = p_d.add_subparsers(dest="daemon_command", metavar="<action>")
    d_run = d_sub.add_parser(
        "run", help="supervise the daemon in the foreground — the tmux "
                    "session command (no install required)")
    d_run.add_argument("name", help="watchdog | watchdog-digest")
    d_ensure = d_sub.add_parser(
        "ensure", help="ensure the daemon's tmux session is alive")
    d_ensure.add_argument("name", nargs="?", default=None,
                          help="daemon or session name (cc-d-<name>)")
    d_stop = d_sub.add_parser("stop", help="kill the daemon's tmux session")
    d_stop.add_argument("name", help="daemon or session name")
    d_status = d_sub.add_parser("status", help="show the daemon's status")
    d_status.add_argument("name", nargs="?", default=None,
                          help="daemon or session name")

    p_pane = sub.add_parser(
        "progress-pane",
        help="run the mode-1 20 fps progress line (Phase 12)")
    p_pane.add_argument("window_id", nargs="?", default=None,
                        help="tmux window id (default: resolve $TMUX_PANE)")

    p_bw = sub.add_parser(
        "blink-watch",
        help="run the mode-0 session-resident waiting-blink poller "
             "(spawned by the SessionStart hook, live bug 2026-11-07)")
    p_bw.add_argument("--pane", required=True,
                      help="tmux pane id of the CC pane ($TMUX_PANE)")
    p_bw.add_argument("--window", required=True,
                      help="tmux window id of the CC pane (@N)")
    p_bw.add_argument("--session-dir", required=True,
                      help="session dir (state.json + ledger.jsonl)")

    p_pp = sub.add_parser(
        "power-plug",
        help="Kasa HS100 power endpoint (Phase 13)")
    pp_sub = p_pp.add_subparsers(dest="power_plug_command", metavar="<action>")
    pp_sub.add_parser("state", help="read the relay state (one-shot)")
    pp_sub.add_parser("on", help="relay on (one-shot, verified)")
    pp_sub.add_parser("off", help="relay off (one-shot, verified)")
    pp_sub.add_parser("cycle", help="off → sleep → on (one-shot)")
    pp_sub.add_parser("serve",
                      help="run the localhost HTTP endpoint (resident)")
    pp_sub.add_parser("install", help="install the launchd LaunchAgent")
    pp_sub.add_parser("uninstall", help="remove the LaunchAgent")
    pp_sub.add_parser("start", help="kickstart the running LaunchAgent")
    pp_sub.add_parser("stop", help="bootout the LaunchAgent")
    pp_sub.add_parser("status", help="show the LaunchAgent status")

    p_sig = sub.add_parser("signal",
                           help="agent signal: needs-human / done (Phase 9)")
    p_sig.add_argument("kind", choices=["needs-human", "done"])
    p_sig.add_argument("--dry-run", action="store_true",
                       help="print the resolved push without sending "
                            "or recording (issue #30)")
    p_sig.add_argument("text",
                       help="reason (needs-human) or summary (done)")

    p_nt = sub.add_parser("notify",
                          help="generic Stop/Notification hook push "
                               "(issue #7)")
    p_nt.add_argument("kind", choices=["stop", "notification"])

    p_ne = sub.add_parser("notify-emit",
                          help="emit one queued notify spec (internal, "
                               "issue #7)")
    p_ne.add_argument("spec_path")

    p_launch = sub.add_parser("launch",
                              help="zsh wrapper backend (Appendix D)")
    p_launch.add_argument("claude_args", nargs=argparse.REMAINDER,
                          help="arguments forwarded to claude")
    return parser


# ------------------------------------------------------------------- status


def _resolve_session(root: Path, key: str) -> tuple[dict | None, dict | None]:
    """``(state record, metric)`` for the current session (PRD §16).

    ``CC_CONTINUITY_SESSION`` (written by the SessionStart hook via
    CLAUDE_ENV_FILE, S11) wins; otherwise the most recent ``runtime/*.json``
    whose ``project_dir`` matches the resolved project root.
    """
    env_sid = os.environ.get("CC_CONTINUITY_SESSION", "").strip()
    if env_sid:
        rec = statusline.find_session_by_id(env_sid)
        if rec is not None and rec.get("project_key") == key:
            return rec, statusline.read_metric(env_sid)

    best: tuple[float, dict] | None = None
    for f in paths.runtime_dir().glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(m, dict) or m.get("project_dir") != str(root):
            continue
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if best is None or ts > best[0]:
            best = (ts, m)
    if best is None:
        return None, None
    rec = statusline.find_session_by_id(best[1].get("session_id"))
    if rec is not None and rec.get("project_key") != key:
        rec = None
    return rec, best[1]


def _fallback_metric(record: dict | None) -> dict | None:
    """#28 ratio fallback: newest registered session's runtime marker.

    Trial runs often have no statusline yet; the project record's session
    list is the only trustworthy pointer then. One source of truth for
    ``ratio`` (#28) and the ``status`` rotation line (#32).
    """
    for s in reversed((record or {}).get("sessions") or []):
        sid = s.get("session_id") if isinstance(s, dict) else None
        if sid:
            metric = statusline.read_metric(str(sid))
            if metric:
                return metric
    return None


def _late_attach(root: Path, key: str, metric: dict | None) -> dict | None:
    """Phase 28: register the running conversation when it has no record.

    The status line's runtime marker carries the live session id even for
    unregistered conversations (e.g. a process started before the Phase 27
    resume registration — 2026-10-03 connector a7c36f64). Returns the
    fresh state record, or None when no marker/sid is available.
    """
    sid = metric.get("session_id") if metric else None
    if not isinstance(sid, str) or not sid:
        return None
    cfg = config.load_config()
    _key, nnn = bootstrap.register_session(root, sid, cfg=cfg)
    print(f"registered {nnn:03d} (no record existed)")
    return state.load_state(_key, nnn)


def collect_status(cwd: str | Path | None = None) -> dict:
    """Gather the ``status`` data (PRD §16). Read-only, never raises."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    cfg = config.load_config(cwd=cwd)

    root = project.resolve_root(cwd)
    key = project.project_key_for_root(root)
    pfile = paths.project_dir(key) / "project.json"
    try:
        project_record = (json.loads(pfile.read_text(encoding="utf-8"))
                          if pfile.is_file() else None)
    except (OSError, json.JSONDecodeError):
        project_record = None

    record, metric = _resolve_session(root, key)
    if not metric:
        metric = _fallback_metric(project_record)
    data = {
        "project_key": key if project_record is not None else None,
        "project_root": str(root),
        "registered": project_record is not None,
        "session": None,
        "used_tokens": metric.get("used_tokens") if metric else None,
        "window_tokens": cfg.get("window_tokens"),
        "ratio": metric.get("ratio") if metric else None,
        "metric_ts": metric.get("ts") if metric else None,
        "metric_stale": statusline.is_stale(metric),
        "next_name": None,
        "latest_checkpoint": (project_record or {}).get("latest_checkpoint"),
        "tmux": bool(os.environ.get("TMUX")),
        "thresholds": {
            "warn_ratio": cfg.get("warn_ratio"),
            "rotate_ratio": cfg.get("rotate_ratio"),
            "hard_ratio": cfg.get("hard_ratio"),
        },
    }
    if record is not None:
        data["session"] = {
            "name": record.get("name"),
            "nnn": record.get("nnn"),
            "session_id": record.get("session_id"),
            "state": record.get("state"),
        }
        if record.get("name"):
            data["next_name"] = naming.successor_name(record["name"])
    # #32: one-line rotation picture (state + live ratio + last threshold
    # crossed). Same ratio source as `ratio` (#28) via _fallback_metric.
    crossed: str | None = None
    r = data["ratio"]
    t = data["thresholds"]
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        if t["hard_ratio"] is not None and r >= t["hard_ratio"]:
            crossed = "hard"
        elif t["rotate_ratio"] is not None and r >= t["rotate_ratio"]:
            crossed = "rotate"
        elif t["warn_ratio"] is not None and r >= t["warn_ratio"]:
            crossed = "warn"
        else:
            crossed = "none"
    rot_state = (record or {}).get("state")
    if rot_state is None and project_record is not None:
        # Same source as the ratio fallback (#28): newest registered
        # session. Its state lives in sessions/<nnn>/state.json.
        for s in reversed(project_record.get("sessions") or []):
            nnn = s.get("nnn") if isinstance(s, dict) else None
            if not isinstance(nnn, int):
                continue
            try:
                srec = json.loads(
                    (paths.session_dir(key, nnn)
                     / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(srec, dict) and srec.get("state"):
                rot_state = srec["state"]
                break
    data["rotation"] = {
        "state": rot_state,
        "ratio": r if crossed is not None else None,
        "last_crossed": crossed,
    }
    return data


def _status_human(data: dict) -> str:
    if (not data["registered"] and data["session"] is None
            and data["ratio"] is None):
        return (f"No cc-continuity data for {data['project_root']} "
                f"(a project registers itself on its first hook).")
    sess = data["session"]
    lines = [f"project    {data['project_root']}"]
    if sess:
        lines.append(f"session    {sess['name']} ({sess['nnn']:03d}) "
                     f"— {sess['session_id']}")
    else:
        lines.append("session    — (none registered yet)")
    if data["ratio"] is not None:
        ratio_line = (f"ratio      {data['ratio'] * 100:.1f}% "
                      f"({data['used_tokens']} / {data['window_tokens']} tokens)")
        if data["metric_stale"]:
            ratio_line += " — STALE (> 10 min)"
        lines.append(ratio_line)
    else:
        lines.append("ratio      — (no metric yet)")
    lines.append(f"state      {sess['state'] if sess else '—'}")
    t = data["thresholds"]
    lines.append(f"thresholds warn {t['warn_ratio'] * 100:.0f}% · "
                 f"rotate {t['rotate_ratio'] * 100:.0f}% · "
                 f"hard {t['hard_ratio'] * 100:.0f}%")
    rot = data.get("rotation") or {}
    rot_ratio = (f"{data['ratio'] * 100:.1f}%"
                 if isinstance(data["ratio"], (int, float))
                 and not isinstance(data["ratio"], bool) else "-")
    lines.append(f"rotation   {rot.get('state') or '-'} · ratio {rot_ratio}"
                 f" · last crossed {rot.get('last_crossed') or '-'}")
    lines.append(f"next       {data['next_name'] or '—'}")
    lines.append(f"checkpoint {data['latest_checkpoint'] or '—'}")
    lines.append(f"tmux       {'yes' if data['tmux'] else 'no'}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    cwd = None
    if getattr(args, "slug", None):
        _key, root, err = _project_by_slug(args.slug)
        if err is not None:
            print(f"cc-continuity status: {err}", file=sys.stderr)
            return 1
        cwd = root
    data = collect_status(cwd=cwd)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(_status_human(data))
    return 0


# -------------------------------------------------------------- statusline


def cmd_statusline(args: argparse.Namespace) -> int:
    """Read the status line payload on stdin, print the composed line."""
    try:
        text = sys.stdin.read()
    except Exception:
        text = ""
    try:
        line = statusline.run(text.encode("utf-8"), config.load_config())
    except Exception:  # the status line must never break the TUI
        line = "◆ …"
    print(line)
    return 0


def cmd_progress_pane(args: argparse.Namespace) -> int:
    """Run the mode-1 progress line in a 1-line pane we own (Phase 12).

    The pane animates ITS window's segment text (per-window data file —
    ``window_id`` comes from the command line, or ``$TMUX_PANE`` when the
    pane was launched without one). ``run_animator`` ticks at ~20 fps until
    any I/O fails, then returns — the process exits, tmux reaps the pane,
    and the status line's self-heal (``ensure_progress_pane``) recreates it
    on the next render.
    """
    win = args.window_id or progress_pane.resolve_window_id()
    # issue #63: the animator polls this session's ledger for the
    # unconsumed needs_human reason (the mode-0 blink_watch poller never
    # spawns in mode 1). Resolve the live session exactly the way
    # cmd_signal resolves it, so writer and reader can never diverge;
    # unresolvable = no poll (pre-#63 behavior).
    sdir = None
    try:
        root = project.resolve_root(Path.cwd())
        key = project.project_key_for_root(root)
        rec, _ = _resolve_session(root, key)
        if rec is not None:
            sdir = str(paths.session_dir(key, int(rec["nnn"])))
    except Exception:
        sdir = None
    # focus dwell (user directive 2026-11-07): real iTerm2 frontmost tty
    # probe; None off-iTerm2 (the watcher then disables dwell itself)
    # app-background re-fire (issue #61): frontmost app name probe for
    # the Dock bounce re-write
    progress_pane.run_animator(window_id=win, session_dir=sdir,
                               _frontmost_tty=blink_watch._default_frontmost_tty,
                               _frontmost_app=blink_watch._default_frontmost_app)
    return 0


def cmd_blink_watch(args: argparse.Namespace) -> int:
    """Run the mode-0 session-resident waiting-blink poller.

    Spawned (detached) by the SessionStart hook via ``blink_watch.spawn``,
    never by hand. It drives one Blinker from the pane's input-box
    region and the ledger's unconsumed needs_human reason, because CC
    does not invoke the status line while it waits on AskUserQuestion
    (live bug 2026-11-07: 46 unanswered minutes without a blink). Quits
    silently when the session leaves the live state, the pane dies, or
    after 12 h.
    """
    try:
        cfg = config.load_config()
    except Exception:
        cfg = {}
    blink_watch.watch(args.session_dir, pane_id=args.pane,
                      window_id=args.window, cfg=cfg,
                      _frontmost_tty=blink_watch._default_frontmost_tty,
                      _frontmost_app=blink_watch._default_frontmost_app)
    return 0


# -------------------------------------------------------------------- hook


def cmd_hook(args: argparse.Namespace) -> int:
    """Read the hook payload on stdin and run the matching handler.

    PRD §9: always exit 0 for the Phase 3 ledger hooks (they never block);
    exceptions are swallowed inside ``common.run_event`` (→ errors.log).
    """
    try:
        text = sys.stdin.read()
    except Exception:
        text = ""
    return common.run_event(args.event, text, HOOK_HANDLERS)


# ----------------------------------------------------------------- history


def cmd_history(args: argparse.Namespace) -> int:
    """Session chain for the current project (PRD §16)."""
    cwd = Path.cwd()
    root = project.resolve_root(cwd)
    key = project.project_key_for_root(root)
    pfile = paths.project_dir(key) / "project.json"
    if not pfile.is_file():
        print(f"No cc-continuity project for {root} "
              f"(a project registers itself on its first hook).")
        return 0
    try:
        record = json.loads(pfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"cc-continuity history: unreadable project record: {e}",
              file=sys.stderr)
        return 1
    for s in (record.get("sessions") or [])[-args.n:]:
        nnn = s.get("nnn") if isinstance(s.get("nnn"), int) else 0
        print(f"{nnn:03d}  {s.get('name')}  {s.get('session_id')}  "
              f"{s.get('created')}")
    return 0


def _project_record(key: str) -> dict | None:
    pfile = paths.project_dir(key) / "project.json"
    try:
        record = json.loads(pfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _project_by_slug(slug: str) -> tuple[str, Path, str | None]:
    """(key, root, error) for an explicit project slug.

    The #28 matching lives here so `ratio [slug]` and `status [slug]`
    (#48) resolve projects identically; the caller prints the error
    with its own command prefix.
    """
    from . import notify  # local: keep the module import list tight
    pdir = paths.projects_dir()
    matches = [d.name for d in pdir.iterdir()
               if d.is_dir() and notify.project_slug(d.name) == slug] \
        if pdir.is_dir() else []
    if not matches:
        return "", Path(), f"no project {slug!r}"
    if len(matches) > 1:
        return "", Path(), (f"ambiguous slug {slug!r} "
                            f"({', '.join(sorted(matches))})")
    key = matches[0]
    record = _project_record(key)
    if record is None or not record.get("root"):
        return "", Path(), f"unreadable project record for {key}"
    return key, Path(record["root"]), None


def cmd_ratio(args: argparse.Namespace) -> int:
    """One-shot context ratio for trial bookkeeping (issue #28)."""
    from . import notify  # local: keep the module import list tight
    if args.slug:
        key, root, err = _project_by_slug(args.slug)
        if err is not None:
            print(f"cc-continuity ratio: {err}", file=sys.stderr)
            return 1
        record = _project_record(key)  # for the #28 session fallback
    else:
        root = project.resolve_root(Path.cwd())
        key = project.project_key_for_root(root)
        record = _project_record(key)
        if record is None:
            # fall back to the single globally registered project
            pdir = paths.projects_dir()
            regs = [d for d in pdir.iterdir()
                    if d.is_dir() and (d / "project.json").is_file()] \
                if pdir.is_dir() else []
            if len(regs) == 1:
                key = regs[0].name
                record = _project_record(key)
                if record is None or not record.get("root"):
                    print(f"cc-continuity ratio: unreadable project record "
                          f"for {key}", file=sys.stderr)
                    return 1
                root = Path(record["root"])
            else:
                print(f"No cc-continuity project for {root} and "
                      f"{len(regs)} registered: pass a slug.",
                      file=sys.stderr)
                return 1
    _rec, metric = _resolve_session(root, key)
    if not metric:
        # the runtime marker is statusline-owned; trial runs often have no
        # statusline yet: fall back to the newest registered session (#28).
        metric = _fallback_metric(record)
    ratio = (metric or {}).get("ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        print(f"{notify.project_slug(key)}: no metrics yet", file=sys.stderr)
        return 1
    if args.json:
        # one object for scripts and the watchdog (issue #34); failures
        # keep the plain stderr line + rc 1 so callers only parse stdout
        # after checking the exit code.
        print(json.dumps({
            "slug": notify.project_slug(key),
            "session_id": metric.get("session_id"),
            "ratio": ratio,
            "ts": metric.get("ts"),
            "stale": statusline.is_stale(metric),
        }))
        return 0
    print(f"{notify.project_slug(key)} {ratio}")
    return 0


# ---------------------------------------------------------------- ledger


_LEDGER_VALUE_CAP = 60


def _ledger_line(entry: dict) -> str:
    """One compact line per ledger entry (issue #46): ``ts type k=v...``."""
    ts = entry.get("ts")
    when = (time.strftime("%H:%M:%S", time.localtime(ts))
            if isinstance(ts, (int, float)) and not isinstance(ts, bool)
            else "-")
    parts = [when, str(entry.get("type") or "-")]
    for k, v in entry.items():
        if k in ("ts", "type"):
            continue
        sval = " ".join(str(v).split())
        if len(sval) > _LEDGER_VALUE_CAP:
            sval = sval[:_LEDGER_VALUE_CAP - 1] + "…"
        parts.append(f"{k}={sval}")
    return " ".join(parts)


def cmd_ledger(args: argparse.Namespace) -> int:
    """Tail of the current session's ledger (issue #46). Read-only.

    Same session resolution as ``status`` (#32): live marker first, then
    the newest registered session.
    """
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, _metric = _resolve_session(root, key)
    nnn = record.get("nnn") if isinstance(record, dict) else None
    if not isinstance(nnn, int):
        project_record = _project_record(key)
        for s in reversed((project_record or {}).get("sessions") or []):
            if isinstance(s, dict) and isinstance(s.get("nnn"), int):
                nnn = s["nnn"]
                break
    if not isinstance(nnn, int):
        print(f"cc-continuity ledger: no session for {root}",
              file=sys.stderr)
        return 1
    n = args.n if isinstance(args.n, int) and args.n > 0 else 20
    entries = ledger.read(paths.session_dir(key, nnn))
    for e in entries[-n:]:
        if isinstance(e, dict):
            print(_ledger_line(e))
    return 0


# ----------------------------------------------------------- Phase 4 cmds


def cmd_validate_draft(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"cc-continuity validate-draft: {e}", file=sys.stderr)
        return 1
    report = checkpoint.validate_draft(text, cfg)
    if report.ok:
        print(f"DRAFT OK ({report.word_count} words, {report.warnings} redacted)")
        return 0
    # Appendix C.6
    print("CONTINUITY: handoff draft invalid: " + "; ".join(report.problems)
          + f". Fix {path} and end your turn.")
    return 1


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    status, details = validate.validate_checkpoint(Path(args.checkpoint), cfg)
    print(f"VALIDATION: {status}")
    for line in details:
        print(line)
    return 0 if status == "OK" else 1


def _kill_handshake_fault():
    """Test-only deterministic kill seam (checkpoint kill e2e test).

    When ``CC_CONTINUITY_TEST_FAULT_AT`` and ``CC_CONTINUITY_TEST_HANDSHAKE_FD``
    are both set, the finalize ``fault`` callback parks the child at the
    requested step after writing READY to the inherited pipe fd, so the
    parent can SIGKILL at a known mid-run point instead of sleeping and
    guessing. Both unset on every production path: returns None.
    """
    step = os.environ.get("CC_CONTINUITY_TEST_FAULT_AT")
    fd = os.environ.get("CC_CONTINUITY_TEST_HANDSHAKE_FD")
    if step is None or fd is None:
        return None
    target, fdnum = int(step), int(fd)

    def _fault(i: int) -> None:
        if i != target:
            return
        os.write(fdnum, b"READY\n")
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
        os._exit(3)                      # orphan guard if parent forgot
    return _fault


def cmd_checkpoint(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, metric = _resolve_session(root, key)
    if record is None:
        record = _late_attach(root, key, metric)
        if record is None:
            print("No registered session for this project "
                  "(a project registers itself on its first hook).",
                  file=sys.stderr)
            return 1
    try:
        cp_id, degraded = checkpoint.finalize(key, record["nnn"], cfg=cfg,
                                              root=root, metric=metric,
                                              fault=_kill_handshake_fault())
    except (checkpoint.CheckpointError, state.StateNotFoundError) as e:
        print(f"cc-continuity checkpoint: {e}", file=sys.stderr)
        return 1
    final = paths.session_dir(key, record["nnn"]) / "checkpoint" / "handoff.md"
    print(f"CHECKPOINT {cp_id}{' (DEGRADED)' if degraded else ''} → {final}")
    return 0


# ----------------------------------------------------------- Phase 5 cmds


def cmd_request_rotation(args: argparse.Namespace) -> int:
    """Spec §6.8 — manual rotation request from any pre-handoff state."""
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, metric = _resolve_session(root, key)
    if record is None:
        record = _late_attach(root, key, metric)
        if record is None:
            print("No registered session for this project "
                  "(a project registers itself on its first hook).",
                  file=sys.stderr)
            return 1
    nnn = int(record["nnn"])
    ratio = (metric.get("ratio") if metric and not statusline.is_stale(metric)
             else None)
    rec = state.load_state(key, nnn)
    s = rec["state"]
    if s in ("BOOTSTRAPPING", "ACTIVE", "WARNED"):
        cfg = config.load_config()
        rec, _ = thresholds.advance(key, nnn, "ROTATION_PENDING",
                                    ratio=ratio, cfg=cfg)
        print(f"Rotation pending: {rec['name']} -> "
              f"{naming.successor_name(rec['name'])}")
    elif s == "ROTATION_PENDING":
        print(f"Already ROTATION_PENDING ({rec['name']}).")
    else:
        print(f"Already past ROTATION_PENDING (state: {s}).")
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    """P9-D5 / spec §5 rows 7+9 — an agent signal with a push.

    ``needs-human``: urgent push + ``needs_human`` ledger entry (the C.2/
    C.3 texts and the status line then show the block; the model ends its
    turn and waits for the user to remote in). ``done``: default push +
    ``task_done`` ledger entry. NOT a state-machine state (P9-D5). No
    registered session -> stderr + exit 1, no push.
    """
    from . import notify
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, _ = _resolve_session(root, key)
    if record is None:
        print("No registered session for this project "
              "(a project registers itself on its first hook).",
              file=sys.stderr)
        return 1
    nnn = int(record["nnn"])
    slug = notify.project_slug(key)
    cfg = config.load_config()
    if args.dry_run:
        # issue #30: render the exact push, touch nothing (no network,
        # no ledger).
        if args.kind == "needs-human":
            event, fields = "needs_human", {"reason": args.text,
                                            "host": notify.host()}
        else:
            event, fields = "task_done", {"summary": args.text}
        title, body, prio = notify.build_message(event, slug=slug, nnn=nnn,
                                                 **fields)
        on = notify.enabled(cfg)
        print("cc-continuity: dry run (nothing sent, nothing recorded)")
        print(f"enabled: {on}"
              + ("" if on else " — would NOT send (disabled or bad config)"))
        print(f"title: {title}")
        print(f"priority: {prio}")
        print("body:")
        for ln in body.splitlines():
            print(f"  {ln}")
        return 0
    if args.kind == "needs-human":
        sent = notify.safe_send(cfg, "needs_human", slug=slug, nnn=nnn,
                                reason=args.text, host=notify.host(),
                                wait=True)
        entry, label = {"type": "needs_human", "reason": args.text}, "blocked"
    else:
        sent = notify.safe_send(cfg, "task_done", slug=slug, nnn=nnn,
                                summary=args.text, wait=True)
        entry, label = {"type": "task_done", "summary": args.text}, \
            "task complete"
    ledger.append(paths.session_dir(key, nnn), entry)
    print(f"cc-continuity: {label}"
          + (" — push sent" if sent else
             " — push not sent (disabled or failed; see errors.log)"))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Issue #7 B5/B8 — generic Stop/Notification hook push.

    The settings.json Notification/Stop hooks curl a fixed body today; they
    migrate here so the push carries the session envelope + scrubbed
    content. JSON payload on stdin (bad/empty -> ``{}``). ALWAYS exit 0
    (P9-D7 — a dead endpoint must never fail the hook).
    """
    from . import notify
    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cfg = config.load_config()
    if not notify.enabled(cfg):
        return 0
    sid = (str(payload.get("session_id") or "").strip()
           or os.environ.get("CC_CONTINUITY_SESSION", "").strip())
    rec = statusline.find_session_by_id(sid) if sid else None
    key = (rec or {}).get("project_key") or ""
    slug = notify.project_slug(key) if key else ""
    nnn = int((rec or {}).get("nnn") or 0)
    cwd = str(payload.get("cwd") or Path.cwd())
    if args.kind == "stop":
        event = "stop_done"
        fields = {"transcript_path": payload.get("transcript_path")}
    else:
        event = "notification"
        msg = str(payload.get("message") or "")
        if not msg:
            return 0
        digest = hashlib.sha1(msg.encode("utf-8")).hexdigest()[:10]
        bucket = int(time.time() // 60)  # B8: one push per minute
        if notify.dedup_seen(f"notification:{sid}:{digest}:{bucket}"):
            return 0
        fields = {"msg": msg}
    notify.safe_send(cfg, event, slug=slug, nnn=nnn, wait=False,
                     source="cli", sid=sid or None, repo=cwd, **fields)
    return 0


def cmd_notify_emit(args: argparse.Namespace) -> int:
    """Issue #7 B1 — the detached emit child (spawned by safe_send).

    Reads one queued spec, builds the message with the collectors, fires
    curl. ALWAYS exit 0 (P9-D7); a corrupt spec is logged and consumed.
    """
    from . import notify
    path = Path(args.spec_path)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("spec is not an object")
    except (OSError, ValueError) as e:
        notify._log_failure("notify-emit", f"{args.spec_path}: {e}")
        try:
            path.unlink()
        except OSError:
            pass
        return 0
    spec["_path"] = str(path)
    notify.emit_spec(spec)
    return 0


def _toggle_off(args: argparse.Namespace, flag: bool) -> int:
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, _ = _resolve_session(root, key)
    if record is None:
        print("No registered session for this project "
              "(a project registers itself on its first hook).", file=sys.stderr)
        return 1
    rec = state.update_fields(key, int(record["nnn"]), off=flag)
    print(f"cc-continuity {'off' if flag else 'on'}: {rec['name']} "
          f"off={str(rec['off']).lower()}")
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    return _toggle_off(args, True)


def cmd_on(args: argparse.Namespace) -> int:
    return _toggle_off(args, False)


def _repair_resolve_key(arg: str) -> str | None:
    """Accept a project key (dir name under projects/) or a repo path."""
    if (paths.project_dir(arg) / "project.json").is_file():
        return arg
    p = Path(arg).expanduser()
    if p.is_dir():
        key = project.project_key_for_root(project.resolve_root(p))
        if (paths.project_dir(key) / "project.json").is_file():
            return key
    return None


def _repair_nnns(spec: str) -> list[int]:
    return [int(t) for t in spec.split(",") if t.strip()]


def _repair_live(key: str, rec: dict) -> bool:
    """True when the record's session id still reports a fresh metric."""
    sid = rec.get("session_id")
    if not isinstance(sid, str) or not sid:
        return False
    return not statusline.is_stale(statusline.read_metric(sid))


def _repair_ghost(key: str, nnn: int) -> int:
    rec = state.load_state(key, nnn)
    if rec["state"] == "ROTATED":
        print(f"repair: {rec['name']} already terminal, nothing to do")
        return 0
    if _repair_live(key, rec):
        print(f"repair: refuse ghost {rec['name']}: metric is fresh (live)",
              file=sys.stderr)
        return 1
    had_conversation = any(e.get("type") == "user_prompt"
                           for e in ledger.read(paths.session_dir(key, nnn)))
    if had_conversation:
        print(f"repair: refuse ghost {rec['name']}: ledger holds a "
              "conversation (use --close instead)", file=sys.stderr)
        return 1
    state.update_fields(key, nnn, off=True)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "state",
                   "fields": {"repair": "ghost-off"}})
    print(f"repair: {rec['name']} ghost-off (never had a conversation)")
    return 0


def _repair_close(key: str, nnn: int, reason: str) -> int:
    rec = state.load_state(key, nnn)
    if _repair_live(key, rec):
        print(f"repair: refuse close {rec['name']}: metric is fresh (live)",
              file=sys.stderr)
        return 1
    before = rec["state"]
    state.close_dead(key, nnn, reason)
    if before == "ROTATED":
        print(f"repair: {rec['name']} already terminal, nothing to do")
    else:
        print(f"repair: {rec['name']} closed {before} -> ROTATED "
              f"({reason})")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Admin repair for junk ledger records (user directive 2026-11-03:
    cleanup only through sanctioned tooling, never hand-edits)."""
    if args.repair_command != "sessions":
        print("usage: cc-continuity repair sessions ...", file=sys.stderr)
        return 2
    key = _repair_resolve_key(args.project_arg)
    if key is None:
        print(f"repair: unknown project {args.project_arg!r} "
              "(no registered ledger)", file=sys.stderr)
        return 1
    rc = 0
    for nnn in _repair_nnns(args.ghost):
        try:
            rc |= _repair_ghost(key, nnn)
        except state.StateNotFoundError:
            print(f"repair: no record {nnn:03d} for {key}", file=sys.stderr)
            rc = 1
    for nnn in _repair_nnns(args.close):
        try:
            rc |= _repair_close(key, nnn, args.reason)
        except state.StateNotFoundError:
            print(f"repair: no record {nnn:03d} for {key}", file=sys.stderr)
            rc = 1
    return rc


def cmd_rotate(args: argparse.Namespace) -> int:
    """Rotator entry (PRD §11/§16) — spawned detached by the Stop hook."""
    from . import rotator  # local import: rotator imports hooks.common
    rec = statusline.find_session_by_id(args.session)
    if rec is None:
        print("No registered session for this session id "
              "(a project registers itself on its first hook).",
              file=sys.stderr)
        return 1
    key = rec["project_key"]
    nnn = int(rec["nnn"])
    try:
        pfile = paths.project_dir(key) / "project.json"
        root = json.loads(pfile.read_text(encoding="utf-8"))["root"]
    except (OSError, json.JSONDecodeError, KeyError):
        print(f"cc-continuity rotate: unreadable project record for {key}",
              file=sys.stderr)
        return 1
    cfg = config.load_config(cwd=root)
    return rotator.run(key=key, nnn=nnn, session_id=args.session,
                       pane=args.pane, rotation_id=args.rotation_id, cfg=cfg)


def cmd_rotate_retry(args: argparse.Namespace) -> int:
    """Self-retry entry (issue #53) — spawned detached by the rotator."""
    from . import rotator  # local import: rotator imports hooks.common
    key = args.key
    nnn = int(args.nnn)
    try:
        pfile = paths.project_dir(key) / "project.json"
        root = json.loads(pfile.read_text(encoding="utf-8"))["root"]
    except (OSError, json.JSONDecodeError, KeyError):
        print(f"cc-continuity rotate-retry: unreadable project record for "
              f"{key}", file=sys.stderr)
        return 1
    cfg = config.load_config(cwd=root)
    return rotator.retry_run(key=key, nnn=nnn, session_id=args.session,
                             pane=args.pane, rotation_id=args.rotation_id,
                             attempt=args.attempt, cfg=cfg)


def cmd_launch(args: argparse.Namespace) -> int:
    from . import launch
    argv = list(args.claude_args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    return launch.run_launch(argv, unattended=args.unattended)


# ------------------------------------------------------------ Phase 7 cmds


USAGE_SUMMARY = """\
Daily use:
  cd ~/anything
  claude                       # auto-tmux, named <project>-NNN
  watch the status line: ◆ NN% Active Usage → PENDING → rotation
In-session skills:
  /continuity-status  /continuity-checkpoint  /continuity-rotate
  /continuity-off     /continuity-on          /continuity-history
Disable for one repo: .claude/continuity.json {"enabled": false}
Disable for one launch: CC_CONTINUITY=0 claude
Skip tmux for one launch: CC_CONTINUITY_NO_TMUX=1 claude (assisted mode)
Uninstall: cc-continuity uninstall
"""


def cmd_install(args: argparse.Namespace) -> int:
    from . import install  # local import keeps cli import light
    try:
        ntfy = install.resolve_ntfy(
            args.ntfy, is_tty=sys.stdin.isatty() and sys.stdout.isatty())
        install.install(Path.home(), window=args.window, ntfy=ntfy)
    except install.InstallError as e:
        print(f"cc-continuity install: {e}", file=sys.stderr)
        return 1
    print("cc-continuity installed.")
    for line in USAGE_SUMMARY.splitlines():
        print("  " + line)
    print("\ndoctor:")
    from . import doctor
    checks = doctor.run_doctor(Path.home())
    print(doctor.render(checks))
    return 0 if all(c.ok for c in checks) else 1


def cmd_configure(args: argparse.Namespace) -> int:
    """P9-D2: post-install config changes — no reinstall needed."""
    from . import atomic, install, paths
    cfg_path = paths.config_path()
    if args.show:
        if (args.ntfy is not None or args.unattended
                or args.watchdog_health is not None
                or args.watchdog_enabled is not None):
            print("cc-continuity configure: --show is read-only (cannot "
                  "combine with change flags)", file=sys.stderr)
            return 1
        missing = not cfg_path.is_file()
        # The installer stamp block is noise when asking "what does this
        # session actually run on?" (issue #49); never written here.
        shown = {k: v for k, v in config.load_config().items()
                 if k != "install"}
        if args.json:
            print(json.dumps(shown, sort_keys=True))
        else:
            for key in sorted(shown):
                val = shown[key]
                print(key, val if isinstance(val, str) else json.dumps(val))
        if missing:
            print("cc-continuity configure: no config file yet (showing "
                  "the built-in defaults)", file=sys.stderr)
        return 0
    cfg: dict = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"cc-continuity configure: unreadable {cfg_path}: {e}",
                  file=sys.stderr)
            return 1
        if not isinstance(loaded, dict):
            print(f"cc-continuity configure: {cfg_path} must be a JSON "
                  "object", file=sys.stderr)
            return 1
        cfg = loaded
    changed = []
    if args.ntfy is not None:
        try:
            endpoint = ("" if args.ntfy.strip().lower() == "off"
                        else install.validate_endpoint(args.ntfy))
        except install.InstallError as e:
            print(f"cc-continuity configure: {e}", file=sys.stderr)
            return 1
        cfg["notify_endpoint"] = endpoint
        cfg["notify_enabled"] = bool(endpoint)
        changed.append("notify_endpoint=" + (endpoint or "(off)"))
    if args.unattended:
        cfg["unattended"] = True
        changed.append("unattended=true")
    if args.watchdog_health is not None:
        url = ("" if args.watchdog_health.strip().lower() == "off"
               else args.watchdog_health.strip())
        if url and not url.startswith(("http://", "https://")):
            print(f"cc-continuity configure: invalid health URL "
                  f"{url!r} (expected http(s)://… or 'off')",
                  file=sys.stderr)
            return 1
        cfg["watchdog_health_url"] = url
        changed.append("watchdog_health_url="
                       + (url or "(liveness-only)"))
    if args.watchdog_enabled is not None:
        cfg["watchdog_enabled"] = args.watchdog_enabled == "true"
        changed.append("watchdog_enabled=" + args.watchdog_enabled)
    if not changed:
        print("cc-continuity configure: nothing to change (pass --ntfy "
              "<url|off> and/or --unattended)", file=sys.stderr)
        return 1
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic.atomic_write_json(cfg_path, cfg)
    print("cc-continuity configured: " + ", ".join(changed))
    return 0


def cmd_watchdog(args: argparse.Namespace, home: Path | None = None,
                 runner=subprocess.run) -> int:
    """P10-D2: the watchdog CLI — host lifecycle + foreground run.

    The five lifecycle actions route through the selected host
    (select_host): macOS → LaunchdHost → the exact existing plist trio
    (behavior unchanged); WSL/Linux → systemd / tmux (Task 5).
    """
    from . import hosting, watchdogd  # local import keeps cli light (P10-P1)
    home = Path(home) if home is not None else Path.home()
    action = args.watchdog_command
    if action == "digest":
        return cmd_watchdog_digest(args, home=home, runner=runner)
    if action == "run":
        return watchdogd.run_forever(config.load_config())
    host = hosting.select_host(runner=runner)
    if action == "install":
        if not config.load_config().get("watchdog_enabled"):
            print("cc-continuity watchdog: watchdog_enabled is false — "
                  "enable it first (cc-continuity configure "
                  "--watchdog-enabled true), then install: a disabled "
                  "daemon exits 0 on every launchd spawn",
                  file=sys.stderr)
            return 1
        try:
            p = host.install("watchdog", home)
        except watchdogd.WatchdogError as e:
            print(f"cc-continuity watchdog install: {e}", file=sys.stderr)
            return 1
        print(f"watchdog installed: {p} "
              f"({_host_note(host, 'launchd RunAtLoad + KeepAlive')})")
        return 0
    if action == "uninstall":
        host.uninstall("watchdog", home)
        print("watchdog uninstalled ("
              + _host_note(host, "launchd bootout + plist removed") + ")")
        return 0
    if action == "start":
        ok = host.start("watchdog", home)
        msg = "watchdog started" if ok \
            else "watchdog start failed (is it installed?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "stop":
        ok = host.stop("watchdog", home)
        msg = "watchdog stopped" if ok \
            else "watchdog stop failed (was it loaded?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "status":
        out = host.status("watchdog", home)
        print("watchdog: not loaded" if out is None else out.rstrip())
        return 0
    return 2


def _daemon_name(name: str) -> str:
    # the rc self-heal line (spec §7.4) passes the SESSION name
    return name[len("cc-d-"):] if name.startswith("cc-d-") else name


def _host_note(host, macos: str) -> str:
    """Lifecycle print suffix: macOS keeps the historical launchd wording
    byte-identical; other hosts name their mechanism (tmux / systemd)."""
    kind = host.kind()
    return macos if kind == "launchd" else kind


def cmd_daemon(args: argparse.Namespace, home: Path | None = None,
               runner=subprocess.run) -> int:
    """Spec §7: the tmux-hosted daemons (WSL/Linux).

    ``run`` is the tmux session command and never needs an install;
    ensure/stop/status route through the selected host (select_host is
    a Task-5 stub until then — the T4 tests exercise TmuxHost directly,
    not this CLI).
    """
    from . import daemon_supervisor, hosting  # local import keeps cli light
    home = Path(home) if home is not None else Path.home()
    action = args.daemon_command
    if action == "run":
        name = _daemon_name(args.name)
        if name not in daemon_supervisor.DAEMON_ARGV:
            print(f"cc-continuity daemon run: unknown daemon {name!r}",
                  file=sys.stderr)
            return 2
        return daemon_supervisor.run_supervisor(name, home)
    if action == "ensure":
        if args.name is None:
            names = [n for n in daemon_supervisor.DAEMON_ARGV
                     if daemon_supervisor.state_path(n, home).is_file()]
            if not names:
                if hosting.kind() == "macos":
                    print("launchd hosts the daemons (macOS)")
                    return 0
                print("no daemons installed")
                return 1
            host = hosting.select_host()
            for n in names:
                host.ensure(n, home)
            return 0
        name = _daemon_name(args.name)
        if not daemon_supervisor.state_path(name, home).is_file():
            print(f"daemon {args.name}: not installed here", file=sys.stderr)
            return 1
        hosting.select_host().ensure(name, home)
        return 0
    host = hosting.select_host()
    if action == "stop":
        ok = host.stop(_daemon_name(args.name), home)
        print("daemon stopped" if ok
              else "daemon stop failed (was it running?)",
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "status":
        if args.name is None:
            for n in daemon_supervisor.DAEMON_ARGV:
                if daemon_supervisor.state_path(n, home).is_file():
                    print(f"{n}: {host.status(n, home)}")
            return 0
        print(host.status(_daemon_name(args.name), home))
        return 0
    return 2


def cmd_watchdog_digest(args: argparse.Namespace, home: Path | None = None,
                        runner=subprocess.run) -> int:
    """Phase 13: the digest relay CLI — foreground passes + the periodic
    agent's host lifecycle (routed like cmd_watchdog; macOS unchanged)."""
    from . import hosting, watchdogd, watchdog_digest  # local: keep cli light
    home = Path(home) if home is not None else Path.home()
    action = args.digest_command
    if action == "run-loop":
        return watchdog_digest.run_loop()
    if action == "run":
        res = watchdog_digest.run_once()
        print(json.dumps(res))
        return 0
    host = hosting.select_host(runner=runner)
    if action == "install":
        try:
            p = host.install("watchdog-digest", home)
        except (watchdog_digest.DigestError, watchdogd.WatchdogError) as e:
            print(f"cc-continuity watchdog digest install: {e}",
                  file=sys.stderr)
            return 1
        print(f"digest agent installed: {p} "
              f"({_host_note(host, 'launchd RunAtLoad + StartInterval 120 s')})")
        return 0
    if action == "uninstall":
        host.uninstall("watchdog-digest", home)
        print("digest agent uninstalled ("
              + _host_note(host, "launchd bootout + plist removed") + ")")
        return 0
    if action == "start":
        ok = host.start("watchdog-digest", home)
        msg = "digest agent kickstarted (one pass now)" if ok \
            else "digest agent start failed (is it installed?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "stop":
        ok = host.stop("watchdog-digest", home)
        msg = "digest agent stopped" if ok \
            else "digest agent stop failed (was it loaded?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "status":
        out = host.status("watchdog-digest", home)
        print("digest agent: not loaded" if out is None else out.rstrip())
        return 0
    return 2


def cmd_power_plug(args: argparse.Namespace, home: Path | None = None,
                   runner=subprocess.run) -> int:
    """Phase 13: the power-plug CLI — one-shot relay actions (also the
    manual test tools) + the resident endpoint's plist lifecycle.

    macOS-only (Kasa + launchd): the guard runs first, before any plist
    work — and before reading the subcommand attribute (spec §7.2).
    """
    from . import hosting  # local import keeps cli import light
    if hosting.kind() != "macos":
        print("power-plug is a macOS-only feature (Kasa)", file=sys.stderr)
        return 1
    from . import power_plug  # local import keeps cli import light
    action = args.power_plug_command
    if action in ("state", "on", "off", "cycle"):
        cfg = config.load_config()
        if action == "state":
            res = power_plug.read_state(cfg)
        elif action == "on":
            res = power_plug.set_relay_state(cfg, on=True)
        elif action == "off":
            res = power_plug.set_relay_state(cfg, on=False)
        else:
            res = power_plug.cycle(cfg)
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    if action == "serve":
        power_plug.serve(config.load_config())
        return 0
    home = Path(home) if home is not None else Path.home()
    if action == "install":
        try:
            p = power_plug.install_plist(home, runner=runner)
        except power_plug.PowerPlugError as e:
            print(f"cc-continuity power-plug install: {e}", file=sys.stderr)
            return 1
        print(f"power-plug endpoint installed: {p} "
              "(launchd RunAtLoad + KeepAlive)")
        return 0
    if action == "uninstall":
        power_plug.uninstall_plist(home, runner=runner)
        print("power-plug endpoint uninstalled "
              "(launchd bootout + plist removed)")
        return 0
    if action == "start":
        ok = power_plug.start_plist(home, runner=runner)
        msg = "power-plug endpoint started" if ok \
            else "power-plug endpoint start failed (is it installed?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "stop":
        ok = power_plug.stop_plist(home, runner=runner)
        msg = "power-plug endpoint stopped" if ok \
            else "power-plug endpoint stop failed (was it loaded?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "status":
        out = power_plug.status_plist(home, runner=runner)
        print("power-plug endpoint: not loaded" if out is None else out.rstrip())
        return 0
    return 2


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import daemon_supervisor, hosting, install
    home = Path.home()
    if hosting.kind() != "macos":
        # host-managed daemons (a state file ⇔ installed): stop + clean
        # before the file cleanup (macOS: no state files → no-op)
        host = hosting.select_host()
        for name in daemon_supervisor.DAEMON_ARGV:
            if daemon_supervisor.state_path(name, home).is_file():
                host.uninstall(name, home)
    try:
        install.uninstall(home, purge=args.purge)
    except install.UninstallError as e:
        print(f"cc-continuity uninstall: {e}", file=sys.stderr)
        return 1
    print("cc-continuity uninstalled"
          + (" (store purged)." if args.purge
             else " (store kept: ~/.claude-continuity/projects)."))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor
    checks = doctor.run_doctor(Path.home())
    if args.json:
        import dataclasses
        print(json.dumps([dataclasses.asdict(c) for c in checks], indent=2))
    else:
        print(doctor.render(checks))
    return 0 if all(c.ok for c in checks) else 1


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "launch":
        # launch forwards its args verbatim to claude; argparse would
        # misread leading-dash args (e.g. `-p`) as cc-continuity options,
        # so the subcommand is split by hand (cmd_launch strips a leading
        # `--`, which the zsh wrapper always sends).
        rest = argv[1:]
        unattended = "--unattended" in rest
        if unattended:
            rest = [a for a in rest if a != "--unattended"]
        return cmd_launch(argparse.Namespace(
            command="launch", claude_args=rest, unattended=unattended))
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    if args.command == "statusline":
        return cmd_statusline(args)
    if args.command == "progress-pane":
        return cmd_progress_pane(args)
    if args.command == "blink-watch":
        return cmd_blink_watch(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "ratio":
        return cmd_ratio(args)
    if args.command == "ledger":
        return cmd_ledger(args)
    if args.command == "validate-draft":
        return cmd_validate_draft(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "checkpoint":
        return cmd_checkpoint(args)
    if args.command == "request-rotation":
        return cmd_request_rotation(args)
    if args.command == "signal":
        return cmd_signal(args)
    if args.command == "notify":
        return cmd_notify(args)
    if args.command == "notify-emit":
        return cmd_notify_emit(args)
    if args.command == "off":
        return cmd_off(args)
    if args.command == "on":
        return cmd_on(args)
    if args.command == "repair":
        return cmd_repair(args)
    if args.command == "rotate":
        return cmd_rotate(args)
    if args.command == "rotate-retry":
        return cmd_rotate_retry(args)
    if args.command == "launch":
        return cmd_launch(args)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "configure":
        return cmd_configure(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "watchdog":
        return cmd_watchdog(args)
    if args.command == "power-plug":
        return cmd_power_plug(args)
    if args.command == "daemon":
        return cmd_daemon(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
