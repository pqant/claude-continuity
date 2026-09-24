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
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import (__version__, checkpoint, config, ledger, naming,
               paths, project, progress_pane, state, statusline,
               thresholds, validate)
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
    sub.add_parser("statusline", help="status line entry (PRD §7)")

    p_hook = sub.add_parser("hook", help="hook entry (PRD §9)")
    p_hook.add_argument("event",
                        help="hook event name (e.g. UserPromptSubmit)")

    p_hist = sub.add_parser("history", help="session chain for this project")
    p_hist.add_argument("-n", type=int, default=20,
                        help="show at most N most recent sessions (default 20)")

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

    p_rot = sub.add_parser("rotate", help="rotator (PRD §11)")
    p_rot.add_argument("--session", required=True,
                       help="Claude Code session id being rotated")
    p_rot.add_argument("--pane", required=True,
                       help="tmux pane target (TMUX_PANE)")
    p_rot.add_argument("--rotation-id", required=True, dest="rotation_id",
                       help="rotation id written by the Stop hook")

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

    p_wd = sub.add_parser("watchdog",
                          help="LLM watchdog daemon (Phase 10)")
    wd_sub = p_wd.add_subparsers(dest="watchdog_command", metavar="<action>")
    wd_sub.add_parser("install", help="install the launchd LaunchAgent")
    wd_sub.add_parser("uninstall", help="remove the LaunchAgent")
    wd_sub.add_parser("start", help="kickstart the running LaunchAgent")
    wd_sub.add_parser("stop", help="bootout the LaunchAgent")
    wd_sub.add_parser("status", help="show the LaunchAgent status")
    wd_sub.add_parser("run", help="run the watchdog loop (foreground)")

    p_pane = sub.add_parser(
        "progress-pane",
        help="run the mode-1 20 fps progress line (Phase 12)")
    p_pane.add_argument("window_id", nargs="?", default=None,
                        help="tmux window id (default: resolve $TMUX_PANE)")

    p_sig = sub.add_parser("signal",
                           help="agent signal: needs-human / done (Phase 9)")
    p_sig.add_argument("kind", choices=["needs-human", "done"])
    p_sig.add_argument("text",
                       help="reason (needs-human) or summary (done)")

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
    lines.append(f"next       {data['next_name'] or '—'}")
    lines.append(f"checkpoint {data['latest_checkpoint'] or '—'}")
    lines.append(f"tmux       {'yes' if data['tmux'] else 'no'}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    data = collect_status()
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
        line = "⟳ …"
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
    progress_pane.run_animator(window_id=win)
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


def cmd_checkpoint(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    root = project.resolve_root(Path.cwd())
    key = project.project_key_for_root(root)
    record, metric = _resolve_session(root, key)
    if record is None:
        print("No registered session for this project "
              "(a project registers itself on its first hook).",
              file=sys.stderr)
        return 1
    try:
        cp_id, degraded = checkpoint.finalize(key, record["nnn"], cfg=cfg,
                                              root=root, metric=metric)
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
        print("No registered session for this project "
              "(a project registers itself on its first hook).", file=sys.stderr)
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
    """P10-D2: the watchdog CLI — plist lifecycle + foreground run."""
    from . import watchdogd  # local import keeps cli import light (P10-P1)
    home = Path(home) if home is not None else Path.home()
    action = args.watchdog_command
    if action == "run":
        return watchdogd.run_forever(config.load_config())
    if action == "install":
        if not config.load_config().get("watchdog_enabled"):
            print("cc-continuity watchdog: watchdog_enabled is false — "
                  "enable it first (cc-continuity configure "
                  "--watchdog-enabled true), then install: a disabled "
                  "daemon exits 0 on every launchd spawn",
                  file=sys.stderr)
            return 1
        try:
            p = watchdogd.install_plist(home, runner=runner)
        except watchdogd.WatchdogError as e:
            print(f"cc-continuity watchdog install: {e}", file=sys.stderr)
            return 1
        print(f"watchdog installed: {p} (launchd RunAtLoad + KeepAlive)")
        return 0
    if action == "uninstall":
        watchdogd.uninstall_plist(home, runner=runner)
        print("watchdog uninstalled (launchd bootout + plist removed)")
        return 0
    if action == "start":
        ok = watchdogd.start_plist(home, runner=runner)
        msg = "watchdog started" if ok \
            else "watchdog start failed (is it installed?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "stop":
        ok = watchdogd.stop_plist(home, runner=runner)
        msg = "watchdog stopped" if ok \
            else "watchdog stop failed (was it loaded?)"
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if action == "status":
        out = watchdogd.status_plist(home, runner=runner)
        print("watchdog: not loaded" if out is None else out.rstrip())
        return 0
    return 2


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import install
    try:
        install.uninstall(Path.home(), purge=args.purge)
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
    if args.command == "status":
        return cmd_status(args)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "history":
        return cmd_history(args)
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
    if args.command == "off":
        return cmd_off(args)
    if args.command == "on":
        return cmd_on(args)
    if args.command == "rotate":
        return cmd_rotate(args)
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
