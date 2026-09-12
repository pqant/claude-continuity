"""``python -m cc_continuity`` → the cc-continuity CLI (PRD §16)."""

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
