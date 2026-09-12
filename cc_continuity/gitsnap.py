"""Git tree snapshot — non-invasive (PRD §10.3).

Captures the *entire* working tree (modifications, staged, untracked;
``.gitignore`` respected) into a detached commit under
``refs/continuity/<slug>/cp-<NNN>`` without touching the real index, the
worktree, or any branch:

    tmp_index = mktemp;  cp <git-path index> tmp_index   (best effort)
    GIT_INDEX_FILE=tmp_index git add -A
    tree     = GIT_INDEX_FILE=tmp_index git write-tree
    commit   = git commit-tree $tree [-p HEAD] -m "continuity cp-<NNN> (<slug>)"
    git update-ref refs/continuity/<slug>/cp-<NNN> $commit

``git rev-parse --git-path index`` (not ``.git/index``) keeps this correct in
linked worktrees. Repos with no commits omit ``-p HEAD``. Snapshot commits
use a fixed identity so they never depend on the user's git config.
These refs are not in any default refspec and are never pushed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import naming

# Fixed identity for snapshot commits (user config may be absent in tests).
_IDENTITY = {
    "GIT_AUTHOR_NAME": "cc-continuity",
    "GIT_AUTHOR_EMAIL": "cc-continuity@localhost",
    "GIT_COMMITTER_NAME": "cc-continuity",
    "GIT_COMMITTER_EMAIL": "cc-continuity@localhost",
}


class GitSnapError(RuntimeError):
    """Git snapshot could not be taken (non-git dir, git failure)."""


def _git(cwd: Path, *args: str, env_extra: dict | None = None,
         check: bool = True) -> tuple[int, str]:
    """Run git; returns (returncode, stdout). ``check`` raises on rc != 0."""
    env = {**os.environ, **_IDENTITY, **(env_extra or {})}
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True
    )
    # rstrip only: porcelain output is space-significant (" M path").
    out = proc.stdout.rstrip("\n")
    if check and proc.returncode != 0:
        raise GitSnapError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.returncode, out


def git_root(cwd: str | Path) -> Path | None:
    """Git toplevel containing ``cwd``, or None if not in a repo."""
    cwd = Path(cwd)
    rc, out = _git(cwd, "rev-parse", "--show-toplevel", check=False)
    if rc != 0 or not out:
        return None
    return Path(out)


def is_git_repo(cwd: str | Path) -> bool:
    return git_root(cwd) is not None


def _copy_real_index(cwd: Path, tmp_index: str) -> bool:
    """Copy the real index into the temp one (best effort, PRD §10.3)."""
    rc, out = _git(cwd, "rev-parse", "--git-path", "index", check=False)
    if rc != 0 or not out:
        return False
    src = Path(out)
    if not src.is_absolute():
        src = cwd / src  # worktrees report a cwd-relative path
    if src.is_file():
        shutil.copyfile(src, tmp_index)
        return True
    return False


def _snapshot_tree(cwd: Path) -> str:
    """Tree sha of the current worktree via a throwaway index."""
    fd, tmp_index = tempfile.mkstemp(prefix=".cc-index-")
    os.close(fd)
    try:
        if not _copy_real_index(cwd, tmp_index):
            # No real index to copy (e.g. repo without commits): a 0-byte
            # index file is corrupt to git — let git create a fresh one.
            os.unlink(tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}
        _git(cwd, "add", "-A", env_extra=env)
        _, tree = _git(cwd, "write-tree", env_extra=env)
        return tree
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


def write_tree(cwd: str | Path) -> str:
    """Tree sha of the full working tree (no commit created)."""
    root = git_root(cwd)
    if root is None:
        raise GitSnapError(f"not a git repo: {cwd}")
    return _snapshot_tree(root)


def make_snapshot(cwd: str | Path, slug: str, nnn: int) -> dict:
    """Create ``refs/continuity/<slug>/cp-<NNN>``; returns {ref, commit, tree}."""
    root = git_root(cwd)
    if root is None:
        raise GitSnapError(f"not a git repo: {cwd}")
    padded = naming.pad_nnn(nnn)
    tree = _snapshot_tree(root)
    head_rc, head = _git(root, "rev-parse", "HEAD", check=False)
    # rc matters: an unborn HEAD prints the literal "HEAD" to stdout.
    parent = ["-p", head] if head_rc == 0 else []  # repo without commits
    _, commit = _git(root, "commit-tree", tree, *parent,
                     "-m", f"continuity cp-{padded} ({slug})")
    ref = f"refs/continuity/{slug}/cp-{padded}"
    _git(root, "update-ref", ref, commit)
    return {"ref": ref, "commit": commit, "tree": tree}


def prune_snapshots(cwd: str | Path, slug: str, retention: int = 20) -> list[str]:
    """Keep the newest ``retention`` snapshot refs; delete the rest."""
    root = git_root(cwd)
    if root is None:
        return []
    _, out = _git(root, "for-each-ref", "--format=%(committerdate:unix) %(refname)",
                  f"refs/continuity/{slug}/")
    entries = sorted(
        (int(ts), ref) for ts, ref in
        (line.split(" ", 1) for line in out.splitlines() if line)
    )
    deleted: list[str] = []
    for _, ref in entries[:-retention] if retention else []:
        _git(root, "update-ref", "-d", ref)
        deleted.append(ref)
    return deleted
