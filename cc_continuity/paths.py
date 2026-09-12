"""State store layout (PRD §14): everything lives under one root.

    <home>/
    ├── config.json
    ├── runtime/<session_id>.json, runtime/<session_id>.started
    ├── logs/
    └── projects/<slug>-<sha8>/  (project.json, lock, sessions/<NNN>/…)

The root is ``~/.claude-continuity`` by default; ``CC_CONTINUITY_HOME``
overrides it (read on every call, no caching) so tests can redirect all
state into a temp directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def continuity_home() -> Path:
    """Root of all cc-continuity state ($CC_CONTINUITY_HOME or ~/.claude-continuity)."""
    override = os.environ.get("CC_CONTINUITY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".claude-continuity"


def ensure_root() -> Path:
    """Create the root and the fixed subdirectories; chmod 0700 (PRD §19)."""
    root = continuity_home()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass  # e.g. fs without mode support; ownership is the real boundary
    for sub in ("runtime", "logs", "projects"):
        (root / sub).mkdir(exist_ok=True)
    return root


def runtime_dir() -> Path:
    return ensure_root() / "runtime"


def logs_dir() -> Path:
    return ensure_root() / "logs"


def projects_dir() -> Path:
    return ensure_root() / "projects"


def config_path() -> Path:
    return continuity_home() / "config.json"


def project_dir(project_key: str) -> Path:
    """Directory of one project: projects/<slug>-<sha8(abs root)>."""
    return projects_dir() / project_key


def session_dir(project_key: str, nnn: int) -> Path:
    """Directory of one session: sessions/<NNN> (zero-padded 3+, PRD §13)."""
    return project_dir(project_key) / "sessions" / f"{nnn:03d}"
