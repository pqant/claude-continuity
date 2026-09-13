"""Race-safe file writes (PRD §14: write temp → fsync → os.replace).

Hooks run concurrently (a single model message can fire many parallel tool
calls — Phase 0, S1/N1), so every write goes through a unique temp file in the
same directory, is fsync'd, then swapped into place with ``os.replace``. A
reader always sees either the old or the new complete content, never a torn
mix. A crash may leave a ``.<name>.*.tmp`` residue; ``doctor`` cleans those.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def fsync_dir(path: Path) -> None:
    """fsync a directory so the rename itself is durable (best effort)."""
    try:
        dfd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def atomic_write(path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path``. Parent directory must exist."""
    path = Path(path)
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # "Crash" mid-write: never leave the target half-updated; drop the temp.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    fsync_dir(parent)


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write(path, text.encode(encoding))


def atomic_write_json(path: str | Path, obj: object, indent: int = 2) -> None:
    """Write a JSON document atomically (2-space indent, trailing newline)."""
    text = json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(path, text)
