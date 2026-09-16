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
