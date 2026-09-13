"""Bootstrap validation in the new session (PRD §12). Implemented in Phase 4.

``cc-continuity validate --checkpoint <dir>`` checks: project path exists and
matches; branch/HEAD vs the snapshot (same / fast-forwarded / diverged);
current working-tree snapshot tree sha vs checkpoint tree sha; if different,
the ≤ 50 differing paths; files named in "Next Atomic Action" exist.
Output: one line ``VALIDATION: OK`` or ``VALIDATION: MISMATCH`` + details.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import gitsnap, project

_NAA_RE = re.compile(r"^## Next Atomic Action[ \t]*\r?\n(.*?)(?=^#{1,2} |\Z)",
                     re.M | re.S)


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return out.returncode, out.stdout.strip()


def validate_checkpoint(checkpoint_dir: Path, cfg: dict,
                        root: Path | None = None) -> tuple[str, list[str]]:
    """PRD §12 — compare a checkpoint against the current project.

    Informational differences (HEAD fast-forwarded, clean tree) go into the
    details without forcing MISMATCH; destructive/ambiguous ones do. A tree
    diff is only a MISMATCH when HEAD did not fast-forward — after an
    advance, changed files are the expected trace of the moved work.
    """
    cp = Path(checkpoint_dir)
    try:
        snap = json.loads((cp / "snapshot.json").read_text(encoding="utf-8"))
        handoff = (cp / "handoff.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as e:
        return "MISMATCH", [f"unreadable checkpoint: {e}"]
    if root is None:
        root = project.resolve_root(Path.cwd())
    root = Path(root)
    problems: list = []
    notes: list = []
    p = snap.get("project", {})
    if p.get("root"):
        if not Path(p["root"]).exists():
            problems.append(f"project path missing: {p['root']}")
        elif p["root"] != str(root):
            problems.append(f"project mismatch: checkpoint {p['root']} "
                            f"vs current {root}")
    rc, cur_head = _git(root, "rev-parse", "HEAD")
    cur_head = cur_head if rc == 0 else None
    cp_head = p.get("head")
    head_ff = False
    if cp_head:
        if cur_head is None:
            problems.append(f"HEAD lost: checkpoint {cp_head[:7]}, "
                            "current repo has no commits")
        elif cur_head != cp_head:
            rc, _ = _git(root, "merge-base", "--is-ancestor", cp_head,
                         cur_head)
            if rc == 0:
                head_ff = True
                notes.append(f"HEAD fast-forwarded: {cp_head[:7]} "
                             f"-> {cur_head[:7]}")
            else:
                rc2, _ = _git(root, "merge-base", "--is-ancestor", cur_head,
                              cp_head)
                if rc2 == 0:
                    problems.append(f"HEAD rewound: {cur_head[:7]} is before "
                                    f"checkpoint {cp_head[:7]}")
                else:
                    problems.append(f"HEAD diverged: checkpoint {cp_head[:7]} "
                                    f"vs current {cur_head[:7]}")
    rc, cur_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if p.get("branch") and rc == 0 and p["branch"] != cur_branch:
        problems.append(f"branch changed: {p['branch']} -> {cur_branch}")
    cp_tree = (snap.get("git", {}).get("snapshot") or {}).get("tree")
    if cp_tree:
        try:
            cur_tree = gitsnap.write_tree(root)
        except gitsnap.GitSnapError:
            problems.append("current working-tree snapshot failed")
            cur_tree = None
        if cur_tree is not None:
            if cur_tree == cp_tree:
                notes.append("working tree unchanged since checkpoint")
            else:
                rc, diff = _git(root, "diff", "--name-status", cp_tree,
                                cur_tree)
                if rc == 0:
                    changed = diff.splitlines()
                    label = (f"working tree changed since checkpoint "
                             f"({len(changed)} paths):")
                    if head_ff:
                        notes.append(label)
                        notes.extend(changed[:50])
                    else:
                        problems.append(label)
                        problems.extend(changed[:50])
                else:
                    problems.append(f"checkpoint tree {cp_tree[:7]} no longer "
                                    "present in repo")
    if not snap.get("degraded"):
        m = _NAA_RE.search(handoff)
        if m:
            missing = []
            for tok in re.findall(r"`([^`\n]+)`", m.group(1)):
                tok = tok.strip()
                if (not tok or " " in tok or tok.startswith(("-", "http"))
                        or not re.search(r"[\./]", tok)):
                    continue
                if not (root / tok).exists():
                    missing.append(tok)
            if missing:
                problems.append("files named in Next Atomic Action missing: "
                                + ", ".join(missing[:20]))
    if problems:
        return "MISMATCH", problems + notes
    return "OK", notes
