"""cc-continuity command-line entry point (PRD §16).

Phase 2 implements ``statusline`` (PRD §7) and ``status [--json]``;
Phase 3 implements ``hook <event>`` (PRD §9) and ``history``.
Phase 4 implements ``checkpoint`` / ``validate`` / ``validate-draft``
(PRD §10/§12). The remaining subcommands still report which phase
implements them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

from . import (__version__, checkpoint, config, naming, paths, project,
               state, statusline, thresholds, validate)
from .hooks import (common, pre_compact, pre_tool_use, session_end,
                    session_start, stop, task, tool_use, user_prompt)

# subcommand -> phase that implements it (PRD §20)
SUBCOMMAND_PHASES: dict[str, int] = {
    "install": 7,
    "uninstall": 7,
    "doctor": 7,
}

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

    p_launch = sub.add_parser("launch",
                              help="zsh wrapper backend (Appendix D)")
    p_launch.add_argument("claude_args", nargs=argparse.REMAINDER,
                          help="arguments forwarded to claude")

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
        rec, _ = thresholds.advance(key, nnn, "ROTATION_PENDING", ratio=ratio)
        print(f"Rotation pending: {rec['name']} -> "
              f"{naming.successor_name(rec['name'])}")
    elif s == "ROTATION_PENDING":
        print(f"Already ROTATION_PENDING ({rec['name']}).")
    else:
        print(f"Already past ROTATION_PENDING (state: {s}).")
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
    return launch.run_launch(argv)


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "launch":
        # launch forwards its args verbatim to claude; argparse would
        # misread leading-dash args (e.g. `-p`) as cc-continuity options,
        # so the subcommand is split by hand (cmd_launch strips a leading
        # `--`, which the zsh wrapper always sends).
        return cmd_launch(argparse.Namespace(
            command="launch", claude_args=argv[1:]))
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
    if args.command == "validate-draft":
        return cmd_validate_draft(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "checkpoint":
        return cmd_checkpoint(args)
    if args.command == "request-rotation":
        return cmd_request_rotation(args)
    if args.command == "off":
        return cmd_off(args)
    if args.command == "on":
        return cmd_on(args)
    if args.command == "rotate":
        return cmd_rotate(args)
    if args.command == "launch":
        return cmd_launch(args)
    phase = SUBCOMMAND_PHASES[args.command]
    print(
        f"cc-continuity {args.command}: not implemented yet (Phase {phase})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
