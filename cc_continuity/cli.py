"""cc-continuity command-line entry point (PRD §16).

Phase 2 implements ``statusline`` (PRD §7) and ``status [--json]``;
Phase 3 implements ``hook <event>`` (PRD §9) and ``history``. The remaining
subcommands still report which phase implements them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

from . import __version__, config, naming, paths, project, statusline
from .hooks import (common, session_end, session_start, task, tool_use,
                    user_prompt)

# subcommand -> phase that implements it (PRD §20)
SUBCOMMAND_PHASES: dict[str, int] = {
    "checkpoint": 4,
    "validate": 4,
    "validate-draft": 4,
    "request-rotation": 5,
    "off": 5,
    "on": 5,
    "rotate": 6,
    "install": 7,
    "uninstall": 7,
    "doctor": 7,
    "launch": 7,
}

# event -> handler (PRD §9). Phase 5–6 add PreToolUse/Stop/PreCompact.
HOOK_HANDLERS: dict[str, Callable[[common.HookContext], dict | None]] = {
    "SessionStart": session_start.handle,
    "UserPromptSubmit": user_prompt.handle,
    "PostToolUse": tool_use.handle,
    "PostToolUseFailure": tool_use.handle_failure,
    "TaskCreated": task.handle,
    "TaskCompleted": task.handle,
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

    for name in SUBCOMMAND_PHASES:
        sub.add_parser(name,
                       help=f"implemented in Phase {SUBCOMMAND_PHASES[name]}")
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


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    if args.command == "statusline":
        return cmd_statusline(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "history":
        return cmd_history(args)
    phase = SUBCOMMAND_PHASES[args.command]
    print(
        f"cc-continuity {args.command}: not implemented yet (Phase {phase})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
