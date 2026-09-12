"""Project registry (PRD §6.2, §13, §14).

Projects auto-register on first contact: the first hook in an unknown
directory creates ``projects/<slug>-<sha8(abs root)>/project.json``. There is
no ``init`` command. The session counter lives in ``project.json`` and moves
only under the project lock; it never goes backwards and is never reused,
even after a failed rotation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import atomic, lock, naming, paths


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


def resolve_root(cwd: str | Path) -> Path:
    """Absolute root a directory maps to: git toplevel, else the dir itself."""
    cwd = Path(cwd)
    top = _git_toplevel(cwd)
    return top if top is not None else cwd.resolve()


def project_key_for_root(root: str | Path) -> str:
    """``<slug>-<sha8(abs root)>`` (PRD §14)."""
    root = str(root)
    slug = naming.sanitize_slug(Path(root).name)
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _project_file(cwd: str | Path | None = None, key: str | None = None) -> Path:
    if key is None:
        key = project_key_for_root(resolve_root(cwd))
    return paths.project_dir(key) / "project.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_project(cwd: str | Path | None = None) -> dict:
    """Register the project for ``cwd`` if needed; return its record."""
    root = resolve_root(cwd)
    key = project_key_for_root(root)
    with lock.held(key):
        pfile = _project_file(key=key)
        if pfile.is_file():
            return json.loads(pfile.read_text(encoding="utf-8"))
        record = {
            "slug": naming.slug_for_dir(root),
            "root": str(root),
            "counter": 0,
            "latest_checkpoint": None,
            "sessions": [],
            "created": _now(),
        }
        pfile.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_json(pfile, record)
        return record


def next_counter(key: str, requested: int | None = None) -> int:
    """Advance the project counter and return the new value.

    Normally ``counter + 1``; with ``requested`` (e.g. the wrapper launched
    the session as ``proj-017``) it jumps to at least that value. Never
    backwards, never reused (PRD §13).
    """
    with lock.held(key):
        pfile = _project_file(key=key)
        record = json.loads(pfile.read_text(encoding="utf-8"))
        new = record["counter"] + 1
        if requested is not None:
            new = max(new, requested)
        record["counter"] = new
        atomic.atomic_write_json(pfile, record)
        return new


def record_session(key: str, name: str, session_id: str, nnn: int) -> dict:
    """Append a session to the project history (mapping per PRD §13)."""
    with lock.held(key):
        pfile = _project_file(key=key)
        record = json.loads(pfile.read_text(encoding="utf-8"))
        record["sessions"].append(
            {"name": name, "session_id": session_id, "nnn": nnn, "created": _now()}
        )
        atomic.atomic_write_json(pfile, record)
        return record
