"""Append-only session ledger (``sessions/<NNN>/ledger.jsonl``, PRD §14).

Hooks fire concurrently (S1/N1), so appends use a single ``os.write`` on an
``O_APPEND`` descriptor — POSIX guarantees the offset bump + write of one
call is atomic, so parallel hooks never interleave or tear lines. Each entry
gets a float ``ts`` (epoch seconds) unless the caller supplies one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

LEDGER_NAME = "ledger.jsonl"


def _path(session_dir: str | Path) -> Path:
    return Path(session_dir) / LEDGER_NAME


def append(session_dir: str | Path, entry: dict) -> dict:
    """Append one entry (single atomic line); returns it with ``ts``."""
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    path = _path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return entry


def read(session_dir: str | Path) -> list[dict]:
    """All entries in order; missing ledger → empty list."""
    path = _path(session_dir)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
