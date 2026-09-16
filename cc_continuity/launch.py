"""`cc-continuity launch` — the zsh wrapper backend (PRD §6.4, Appendix D).

``plan_launch`` is pure and exhaustively tested; ``run_launch`` (Task 7)
executes the plan, spawning tmux/claude through overridable binaries
(``CC_CONTINUITY_TMUX_BIN`` / ``CC_CONTINUITY_CLAUDE_BIN`` — the Phase-6
fake-tmux pattern) and exporting ``CC_CONTINUITY_NAME`` (S8/F11).

Name prediction: ``next_counter_for`` reads the project counter without
locking — the AUTHORITATIVE increment happens under lock in SessionStart
(``next_counter(requested=…)``). Two concurrent launches in the same
project may predict the same NNN; SessionStart then assigns the second
the next free counter (documented in LIMITATIONS.md, Phase 9).
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
from pathlib import Path

from . import naming, paths, project

PASS_SUBCOMMANDS = frozenset({
    "agents", "auth", "auto-mode", "doctor", "gateway", "install", "mcp",
    "plugin", "plugins", "project", "setup-token", "ultrareview",
})
PASS_FLAGS = frozenset({
    "-p", "--print", "--version", "-v", "--help", "-h", "--bg",
    "--background", "--bare",
})
NAME_FLAGS = frozenset({"-n", "--name", "-r", "--resume", "-c", "--continue"})
_NAME_PREFIXES = ("--name=", "--resume=", "--continue=")


@dataclasses.dataclass(frozen=True)
class LaunchPlan:
    mode: str               # "passthrough" | "tmux" | "direct"
    claude_args: tuple      # original argv (+ ("-n", name) when computed)
    name: str | None        # computed logical name (None in passthrough/
    #                        # user-supplied-name cases)
    tmux_session: str | None  # only mode == "tmux": "cc-<slug>-<rand4>"
    reason: str


def next_counter_for(cwd: Path) -> int:
    """The NNN the next session in this project will get (prediction only)."""
    root = project.resolve_root(cwd)
    key = project.project_key_for_root(root)
    pfile = paths.project_dir(key) / "project.json"
    try:
        rec = json.loads(pfile.read_text(encoding="utf-8"))
        return int(rec.get("counter", 0)) + 1
    except (OSError, ValueError, TypeError):
        return 1


def _first_positional(argv: list[str]) -> str | None:
    for a in argv:
        if not a.startswith("-"):
            return a
    return None


def plan_launch(argv, *, cwd, in_tmux, interactive,
                disabled: bool = False, no_tmux: bool = False,
                rand=lambda: secrets.token_hex(2)) -> LaunchPlan:
    """Decide how `claude <argv>` should be executed (P7-D9)."""
    argv = tuple(argv)
    if disabled:
        return LaunchPlan("passthrough", argv, None, None,
                          "cc-continuity disabled")
    if no_tmux:
        return LaunchPlan("passthrough", argv, None, None,
                          "CC_CONTINUITY_NO_TMUX set")
    if not interactive:
        return LaunchPlan("passthrough", argv, None, None, "not interactive")
    if any(a in PASS_FLAGS for a in argv):
        return LaunchPlan("passthrough", argv, None, None, "pass-through flag")
    pos = _first_positional(argv)
    if pos in PASS_SUBCOMMANDS:
        return LaunchPlan("passthrough", argv, None, None,
                          f"subcommand {pos}")
    mode = "direct" if in_tmux else "tmux"
    slug = naming.slug_for_dir(cwd)
    has_name = (any(a in NAME_FLAGS for a in argv)
                or any(a.startswith(_NAME_PREFIXES) for a in argv))
    if has_name:
        return LaunchPlan(
            mode, argv, None,
            f"cc-{slug}-{rand()}" if mode == "tmux" else None,
            "user-supplied name/resume flag")
    name = naming.logical_name(slug, next_counter_for(Path(cwd)))
    return LaunchPlan(
        mode, argv + ("-n", name), name,
        f"cc-{slug}-{rand()}" if mode == "tmux" else None,
        f"auto name {name}")


# ------------------------------------------------------------- execution

import shlex
import shutil
import subprocess
import sys

from . import config

ENV_PREFIX_PARTS = ("ANTHROPIC", "CLAUDE")  # env var name prefixes to bake in


def _env_prefix(name: str | None) -> list[str]:
    """`env KEY=VAL …` pairs for the tmux command (P7-D9b).

    A long-running tmux server hands its STALE env to new sessions; baking
    the current process's ANTHROPIC_*/CLAUDE_* vars into the command makes
    the pane independent of the server's age.
    """
    pairs: list[str] = []
    if name:
        pairs.append(f"CC_CONTINUITY_NAME={shlex.quote(name)}")
    for k, v in os.environ.items():
        if v and any(k == p or k.startswith(p + "_")
                     for p in ENV_PREFIX_PARTS) and k != "CC_CONTINUITY_NAME":
            pairs.append(f"{k}={shlex.quote(v)}")
    return pairs


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def _claude_bin() -> str:
    return os.environ.get("CC_CONTINUITY_CLAUDE_BIN") or "claude"


def _exec_claude(claude_bin: str, args: tuple, name: str | None) -> int:
    env = dict(os.environ)
    if name:
        env["CC_CONTINUITY_NAME"] = name
    os.execvpe(claude_bin, [claude_bin, *args], env)
    return 1  # unreachable


def run_launch(argv: list[str], *, cwd: str | Path | None = None) -> int:
    """Execute the launch plan (PRD §6.4, Appendix D). Returns exit code."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    cfg = config.load_config(cwd=cwd)
    plan = plan_launch(
        list(argv), cwd=cwd,
        in_tmux=bool(os.environ.get("TMUX")),
        interactive=(os.environ.get("CC_CONTINUITY_FORCE_INTERACTIVE") == "1"
                     or (sys.stdin.isatty() and sys.stdout.isatty())),
        disabled=not config.is_enabled(cfg),
        no_tmux=bool(os.environ.get("CC_CONTINUITY_NO_TMUX")),
    )
    tmux_bin = _tmux_bin()
    claude_bin = _claude_bin()
    if plan.mode in ("passthrough", "direct"):
        return _exec_claude(claude_bin, plan.claude_args, plan.name)
    # tmux mode
    if shutil.which(tmux_bin) is None:
        print("cc-continuity: tmux not found — falling back to direct launch",
              file=sys.stderr)
        return _exec_claude(claude_bin, plan.claude_args, plan.name)
    prefix = _env_prefix(plan.name)
    cmd = " ".join(
        ["env", *prefix, "command", claude_bin,
         *[shlex.quote(a) for a in plan.claude_args]])
    r = subprocess.run(
        [tmux_bin, "new-session", "-d", "-s", plan.tmux_session,
         "-c", str(cwd), cmd],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(f"cc-continuity: tmux new-session failed: "
              f"{r.stderr.strip() or r.stdout.strip()}", file=sys.stderr)
        return 1
    a = subprocess.run([tmux_bin, "attach-session", "-t", plan.tmux_session])
    if a.returncode != 0:
        print(f"cc-continuity: session started; attach with: "
              f"tmux attach -t {plan.tmux_session}", file=sys.stderr)
        return 0
    return a.returncode
