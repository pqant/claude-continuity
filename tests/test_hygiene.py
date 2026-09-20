"""P9-D8: no personal data in the public tree (code, tests, public docs).

Internal working logs (HANDOFF.md, PROGRESS.md, docs/TRIAL.md,
docs/PRD.md and other dev-only planning/spec docs) are dev-process records, NOT part of
the published product (P9-P12) and are exempt from this scan.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GLOBS = [
    "cc_continuity/**/*.py",
    "tests/**/*",
    "README.md", "LICENSE", "CONTRIBUTING.md", "LIMITATIONS.md",
    "pyproject.toml",
]
BAD = [
    re.compile(r"my-topic"),   # the original personal ntfy topic
    re.compile(r"$HOME"),
    re.compile(r"example.com"),
    re.compile(r"192\.168\."),
]


def test_no_personal_data_in_public_files():
    hits = []
    for glob in PUBLIC_GLOBS:
        for f in ROOT.glob(glob):
            if not f.is_file() or f.name == "test_hygiene.py":
                continue
            if "__pycache__" in f.parts:
                continue  # bytecode of old sources, not the public tree
            for i, line in enumerate(
                    f.read_text(encoding="utf-8",
                                errors="replace").splitlines(), 1):
                for rx in BAD:
                    if rx.search(line):
                        hits.append(f"{f}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)
