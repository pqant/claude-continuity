"""Session naming rules (PRD §13).

Logical name = ``<slug>-<NNN>``:
- ``slug``: sanitized basename of the git root (or cwd if not in git),
  lowercase ``[a-z0-9-]``, ≤ 32 chars;
- ``NNN``: zero-padded 3 digits, widening when needed (``099 → 100``,
  ``999 → 1000``).

A name ending in ``-<digits>`` continues that sequence on the successor
(``hayalet-v2-017 → hayalet-v2-018``); a name without one gets ``-001``
(``auth-work → auth-work-001``).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_FALLBACK_SLUG = "project"
_MAX_SLUG = 32
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_TRAILING_NUM = re.compile(r"-(\d+)$")


def sanitize_slug(base: str) -> str:
    """Lowercase, ``[a-z0-9-]`` only, runs of other chars → one dash, ≤ 32 chars."""
    s = _NON_SLUG.sub("-", base.lower()).strip("-")
    s = s[:_MAX_SLUG].rstrip("-")
    return s or _FALLBACK_SLUG


def pad_nnn(nnn: int) -> str:
    """3-digit zero padding, widening past 999 (``1000`` stays ``1000``)."""
    if nnn < 0:
        raise ValueError(f"nnn must be >= 0, got {nnn}")
    return f"{nnn:03d}"


def logical_name(slug: str, nnn: int) -> str:
    return f"{slug}-{pad_nnn(nnn)}"


def split_name(name: str) -> tuple[str, int | None]:
    """``proj-002`` → (``proj``, 2); ``auth-work`` → (``auth-work``, None)."""
    m = _TRAILING_NUM.search(name)
    if m and m.start() > 0:
        return name[: m.start()], int(m.group(1))
    return name, None


def successor_name(name: str) -> str:
    """The logical name of the successor session for this name."""
    slug, n = split_name(name)
    if n is None:
        return f"{name}-001"
    return logical_name(slug, n + 1)


def _git_toplevel(cwd: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def slug_for_dir(cwd: str | Path) -> str:
    """Slug for a working directory: git root basename, else cwd basename."""
    cwd = Path(cwd)
    root = _git_toplevel(cwd)
    base = root if root is not None else cwd
    return sanitize_slug(base.name)
