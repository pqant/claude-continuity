"""Checkpoint engine (PRD §10). Implemented in Phase 4.

- Draft validation: 9 template headings in order (Appendix B), required
  sections non-empty, ≤ ``handoff_max_words`` words, no redact-pattern
  secrets (PRD §10.1).
- Deterministic snapshot (PRD §10.2) produced by code only.
- Git tree snapshot via :mod:`cc_continuity.gitsnap`.
- Finalization (PRD §10.4): render handoff.md into ``checkpoint.tmp-<pid>/``,
  fsync, SHA256SUMS, re-read/verify, ``os.replace`` → ``checkpoint``; only
  then ``CHECKPOINT_VERIFIED`` + ``latest_checkpoint`` pointer.
- Degraded checkpoint (deterministic parts only, C.7 header) after 3 failed
  handoff attempts.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import redact

#: Appendix B, exact order and exact strings.
TEMPLATE_HEADINGS: tuple[str, ...] = (
    "Objective",
    "Current Task and Exact Stopping Point",
    "Completed This Session",
    "Next Atomic Action",
    "Remaining Plan",
    "Decisions and Rationale",
    "Rejected Approaches — Do Not Repeat",
    "Open Problems, Risks, Questions for the User",
    "Runtime Environment Notes",
)
#: 0-based indices into TEMPLATE_HEADINGS of sections that must be
#: non-empty (PRD §10.1: sections 1, 2, 4 of the template).
REQUIRED_NONEMPTY: tuple[int, ...] = (0, 1, 3)
MIN_REQUIRED_CHARS = 20

#: Appendix C.7 (degraded checkpoint header).
C7 = ("DEGRADED CHECKPOINT: the model did not produce a valid handoff; only "
      "deterministic state is available. Reconstruct intent from the user "
      "prompts and changed files before continuing.")


class CheckpointError(RuntimeError):
    """Checkpoint build/finalization failed (message is user-facing)."""


@dataclass
class DraftReport:
    ok: bool
    problems: list          # hard failures → C.6 text
    warnings: int           # redactions applied (non-failing)
    redacted_text: str
    word_count: int


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _parse_headings(text: str) -> list[tuple[int, str]]:
    out = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip()))
    return out


def _section_bodies(text: str) -> dict:
    """Map level-2 title → body text (up to the next heading of any level)."""
    bodies: dict = {}
    current = None
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 2:
                current = m.group(2).strip()
                bodies.setdefault(current, "")
            else:
                current = None
        elif current is not None:
            bodies[current] += line + "\n"
    return bodies


def validate_draft(text: str, cfg: dict) -> DraftReport:
    """PRD §10.1 — validate a model-written handoff draft.

    ``problems`` holds hard failures (draft unusable, C.6). Secret matches
    are replaced with ``[REDACTED]`` in ``redacted_text`` and counted in
    ``warnings`` — redaction never fails the draft.
    """
    redacted_text, warnings = redact.Redactor(cfg.get("redact_patterns")).redact(text)
    problems: list = []
    h2 = [t for lvl, t in _parse_headings(redacted_text) if lvl == 2]
    for h in TEMPLATE_HEADINGS:
        if h not in h2:
            problems.append(f"missing heading: {h}")
    seen: list = []
    for h in h2:
        if h in TEMPLATE_HEADINGS and h not in seen:
            seen.append(h)
    expected = [h for h in TEMPLATE_HEADINGS if h in h2]
    if seen != expected:
        problems.append(f"headings out of order: got {seen}")
    for h in dict.fromkeys(h2):
        if h in TEMPLATE_HEADINGS and h2.count(h) > 1:
            problems.append(f"duplicate heading: {h}")
    for h in dict.fromkeys(h2):
        if h not in TEMPLATE_HEADINGS:
            problems.append(f"unexpected heading: {h}")
    bodies = _section_bodies(redacted_text)
    for idx in REQUIRED_NONEMPTY:
        h = TEMPLATE_HEADINGS[idx]
        if h in bodies and len(bodies[h].strip()) < MIN_REQUIRED_CHARS:
            problems.append(f"section too short (< {MIN_REQUIRED_CHARS} chars): {h}")
    word_count = len(redacted_text.split())
    max_words = cfg.get("handoff_max_words", 2500)
    if word_count > max_words:
        problems.append(f"too long: {word_count} > {max_words} words")
    return DraftReport(ok=not problems, problems=problems, warnings=warnings,
                       redacted_text=redacted_text, word_count=word_count)


def headings_in_order(text: str) -> bool:
    """True when all 9 template headings appear, in order, among the H2s.

    Used on the FINAL handoff.md, whose appendix adds extra H2s (allowed).
    """
    h2 = [t for lvl, t in _parse_headings(text) if lvl == 2]
    it = iter(h2)
    return all(any(h == t for t in it) for h in TEMPLATE_HEADINGS)


MAX_STATUS_LINES = 200    # PRD §10.2
MAX_DIFF_LINES = 100


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    # rstrip the newline only — leading spaces are significant (porcelain
    # status encodes staged/unstaged in the first two columns).
    return out.returncode, out.stdout.rstrip("\n")


def git_facts(root: Path) -> dict:
    """PRD §10.2 working-tree facts. Non-git dir → nulls/empties (no raise)."""
    facts: dict = {"git_root": None, "branch": None, "head": None,
                   "ahead": None, "behind": None, "status": [], "diff_stat": [],
                   "staged": 0, "unstaged": 0}
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return facts
    facts["git_root"] = top
    rc, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    facts["branch"] = branch or None
    rc, head = _git(root, "rev-parse", "HEAD")
    facts["head"] = head if rc == 0 else None
    rc, upstream = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    if rc == 0 and upstream:
        rc, counts = _git(root, "rev-list", "--left-right", "--count",
                          f"{upstream}...HEAD")
        if rc == 0:
            parts = counts.split()  # tab-separated, e.g. "0\t1"
            if len(parts) == 2:
                facts["behind"], facts["ahead"] = int(parts[0]), int(parts[1])
    rc, status = _git(root, "status", "--porcelain=v1")
    lines = status.splitlines() if rc == 0 else []
    facts["status"] = lines[:MAX_STATUS_LINES]
    staged = unstaged = 0
    for line in lines:
        if len(line) >= 2:
            if line[0] in "MADRC":
                staged += 1
            if line[1] in "MD":
                unstaged += 1
    facts["staged"], facts["unstaged"] = staged, unstaged
    rc, stat = _git(root, "diff", "--stat", "HEAD")
    if rc != 0:                      # unborn HEAD → staged vs empty tree
        rc, stat = _git(root, "diff", "--cached", "--stat")
    facts["diff_stat"] = stat.splitlines()[:MAX_DIFF_LINES] if rc == 0 else []
    return facts
