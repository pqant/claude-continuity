"""cc-continuity command-line entry point (PRD §16).

Phase 1 delivers the skeleton only: parser with every subcommand from PRD §16,
`--version`, and a dispatch that reports which phase implements each command.
Subcommand arguments and behaviour are added in their own phases.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

# subcommand -> phase that implements it (PRD §20)
SUBCOMMAND_PHASES: dict[str, int] = {
    "statusline": 2,
    "status": 2,
    "history": 2,
    "hook": 3,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-continuity",
        description="Zero-touch session rotation for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name in SUBCOMMAND_PHASES:
        sub.add_parser(name, help=f"implemented in Phase {SUBCOMMAND_PHASES[name]}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    phase = SUBCOMMAND_PHASES[args.command]
    print(
        f"cc-continuity {args.command}: not implemented yet (Phase {phase})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
