"""P9-D8 (public release): no personal data in the public tree.

Generic patterns only — no maintainer-specific strings (personal additions
belong in the maintainer's redact_patterns_extra, not in the public test).
Internal dev logs (HANDOFF.md, PROGRESS.md, docs/PRD.md, docs/TRIAL.md,
dev-only planning/spec docs) are class E: they never enter the public tree
(build-time exclusion) and are therefore not scanned here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GLOBS = [
    "cc_continuity/**/*.py",
    "tests/**/*",
    "README.md", "LICENSE", "CONTRIBUTING.md", "LIMITATIONS.md",
    "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
    "pyproject.toml", "config.example.json",
    "docs/USAGE.md", "docs/WATCHDOG.md", "docs/ARCHITECTURE.md",
    ".gitignore",
]

# Private LAN ranges (TEST-NET 192.0.2.x and loopback 127.0.0.x are fine).
LAN = [
    re.compile(r"192\.168\."),
    re.compile(r"10\.0\.1\."),
    re.compile(r"172\.(1[6-9]|2[0-9]|3[01])\."),
]
# Turkish characters: the public tree is 100% English. Code points are
# built via chr() so this file does not contain the characters it scans
# for (the public build's Turkish battery would flag this file).
_TR_K = (0x130, 0x131, 0x11E, 0x11F, 0x15E, 0x15F,
         0xDC, 0xFC, 0xD6, 0xF6, 0xC7, 0xE7)
TURKISH = [re.compile("[" + "".join(chr(c) for c in _TR_K) + "]")]
# Any home-dir path, capturing the user segment.
HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.-]+)")
# The suite exercises path-parsing / slug-derivation with a small set of
# SANCTIONED generic fixture home dirs (fake usernames, never personal data).
# These are the only /Users/<u> or /home/<u> allowed in the public tree; any
# other user segment (e.g. a real home path) is a personal leak.
GENERIC_FIXTURE_USERS = frozenset({"x", "test", "devx"})


def test_no_personal_data_in_public_files():
    hits = []
    for glob in PUBLIC_GLOBS:
        for f in ROOT.glob(glob):
            if not f.is_file() or f.name == "test_hygiene.py":
                continue
            if "__pycache__" in f.parts or "node_modules" in f.parts:
                continue  # generated, not the public tree
            rel = f.relative_to(ROOT)
            for i, line in enumerate(
                    f.read_text(encoding="utf-8",
                                errors="replace").splitlines(), 1):
                for rx in LAN + TURKISH:
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()}")
                users = HOME.findall(line)
                if users and not all(u in GENERIC_FIXTURE_USERS
                                     for u in users):
                    hits.append(f"{rel}:{i}: {line.strip()}")
    # documented exemptions (non-personal test fixtures, dev tree only):
    # 1. test_settings_merge.py: the non-personal Unicode case
    # 2. test_build_public_repo.py: unit tests for the class-E build
    #    tool; they exercise the scanner with its own fixtures (Turkish
    #    nudge phrases, a private-LAN IP). The file is excluded from the
    #    public tree (EXCLUDE_PREFIXES), so this exemption is a no-op in
    #    the public repo (the file does not exist there).
    hits = [h for h in hits
            if not (h.startswith("tests/test_settings_merge.py")
                    and "ñ" in h)
            and not h.startswith("tests/test_build_public_repo.py")]
    assert not hits, "\n".join(hits)
