"""Project lock: ``fcntl.flock`` with a self-describing lock file (PRD §11.1, §14).

The lock file (``projects/<key>/lock``) holds ``{"pid", "ts", "rotation_id"}``
so a crashed holder is diagnosable. Staleness follows the PRD rule: an entry
older than ``STALE_AFTER`` seconds **and** with a dead PID is stale and gets
taken over by recreating the file. A fresh live holder is never taken over —
the kernel already released a truly dead holder's flock, which is the common
crash path; the file check covers the inherited-fd edge case.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

STALE_AFTER = 120.0  # seconds (PRD §11.1)


class LockHeldError(RuntimeError):
    """Raised when a lock could not be acquired within its timeout."""


def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (signal-0 probe)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by someone else
    return True


def is_stale_info(info: object, now: float | None = None) -> bool:
    """Stale = entry older than STALE_AFTER and recorded PID dead (PRD §11.1).

    Unreadable/malformed content is *not* stale: it cannot be proven stale, so
    callers wait until their timeout instead of taking over a live holder.
    """
    if not isinstance(info, dict):
        return False
    ts, pid = info.get("ts"), info.get("pid")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if now is None:
        now = time.time()
    return (now - ts) > STALE_AFTER and not pid_alive(pid)


class ProjectLock:
    """Advisory exclusive lock on one file. One lock file per project."""

    def __init__(self, path: str | Path, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    def acquire(self, rotation_id: str | None = None, timeout: float | None = None) -> bool:
        """Try to take the lock, retrying until ``timeout`` (seconds).

        Returns True on success. Stale entries are taken over (file recreated).
        """
        if timeout is None:
            timeout = self.timeout
        deadline = time.monotonic() + timeout
        while True:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                info = self._read_info()
                os.close(fd)
                if is_stale_info(info):
                    try:
                        os.unlink(self.path)  # takeover: fresh inode on retry
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            else:
                self._write_info(fd, rotation_id)
                self._fd = fd
                return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
        # The file is kept on purpose: its content records the last holder.

    def __enter__(self) -> "ProjectLock":
        if not self.acquire():
            raise LockHeldError(f"lock held: {self.path}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _read_info(self) -> object:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_info(self, fd: int, rotation_id: str | None) -> None:
        info = {"pid": os.getpid(), "ts": time.time(), "rotation_id": rotation_id}
        data = (json.dumps(info) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)  # offset is 0 on a fresh fd; no lseek needed
        os.write(fd, data)
        os.fsync(fd)


@contextmanager
def held(project_key: str, rotation_id: str | None = None, timeout: float = 10.0):
    """Context manager over the standard per-project lock file."""
    from . import paths

    with ProjectLock(paths.project_dir(project_key) / "lock", timeout=timeout) as lock:
        yield lock
