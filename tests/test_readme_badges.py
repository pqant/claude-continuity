"""Issue #73 + #74: README badges rows.

Two shields.io rows under the title are the README's at-a-glance vitals:
GitHub counters first, package and community signals second. Pin the
exact badge lines (image URL + link target) and their position so a
later edit cannot silently drop one or retarget a link.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "README.md").read_text(encoding="utf-8")

BADGES_GITHUB = [
    "[![Forks](https://img.shields.io/github/forks/pqant/claude-continuity"
    "?label=FORKS&logo=github&labelColor=%23333333&color=%23ff8c00)]"
    "(https://github.com/pqant/claude-continuity/forks)",
    "[![Stars](https://img.shields.io/github/stars/pqant/claude-continuity"
    "?label=STARS&logo=github&labelColor=%23333333&color=%234c1)]"
    "(https://github.com/pqant/claude-continuity/stargazers)",
    "[![Issues](https://img.shields.io/github/issues/pqant/claude-continuity"
    "?label=ISSUES&logo=github&labelColor=%23333333&color=%23007ec6)]"
    "(https://github.com/pqant/claude-continuity/issues)",
    "[![Last commit](https://img.shields.io/github/last-commit/"
    "pqant/claude-continuity?label=LAST%20COMMIT&logo=github"
    "&labelColor=%23333333&color=%23e0559a)]"
    "(https://github.com/pqant/claude-continuity/commits/main)",
    "[![Code of conduct](https://img.shields.io/badge/CODE%20OF%20CONDUCT"
    "-ENFORCED-%239146ff?logo=contributorcovenant&labelColor=%23333333)]"
    "(CODE_OF_CONDUCT.md)",
    "[![Contributions](https://img.shields.io/badge/CONTRIBUTIONS-WELCOME"
    "-%23e0559a?logo=git&labelColor=%23333333)](CONTRIBUTING.md)",
    "[![Security](https://img.shields.io/badge/SECURITY-POLICY-%234c1"
    "?logo=github&labelColor=%23333333)](SECURITY.md)",
]

BADGES_PACKAGE = [
    "[![PyPI](https://img.shields.io/pypi/v/cc-continuity?label=PYPI"
    "&logo=pypi&logoColor=white&labelColor=%23333333&color=%23007ec6)]"
    "(https://pypi.org/project/cc-continuity/)",
    "[![Downloads](https://img.shields.io/pypi/dm/cc-continuity"
    "?label=DOWNLOADS&logo=pypi&logoColor=white&labelColor=%23333333"
    "&color=%23007ec6)](https://pypi.org/project/cc-continuity/)",
    "[![License](https://img.shields.io/github/license/pqant/claude-continuity"
    "?label=LICENSE&logo=github&labelColor=%23333333&color=%234c1)](LICENSE)",
    "[![CI](https://github.com/pqant/claude-continuity/actions/workflows/"
    "ci.yml/badge.svg)](https://github.com/pqant/claude-continuity/actions"
    "/workflows/ci.yml)",
    "[![Python versions](https://img.shields.io/pypi/pyversions/"
    "cc-continuity?label=PYTHON&logo=python&logoColor=white"
    "&labelColor=%23333333&color=%233776ab)]"
    "(https://pypi.org/project/cc-continuity/)",
    "[![Good first issues](https://img.shields.io/github/issues/"
    "pqant/claude-continuity/good%20first%20issue?label=GOOD%20FIRST%20ISSUES"
    "&logo=github&labelColor=%23333333&color=%237057ff)]"
    "(https://github.com/pqant/claude-continuity/issues?q=is%3Aissue"
    "+is%3Aopen+label%3A%22good+first+issue%22)",
]


def test_badges_rows_are_the_first_block():
    lines = TEXT.splitlines()
    assert lines[0] == "# cc-continuity"
    assert lines[1] == ""
    for start, count in ((2, len(BADGES_GITHUB)),
                         (2 + len(BADGES_GITHUB) + 1, len(BADGES_PACKAGE))):
        row = lines[start:start + count]
        assert all(ln.startswith("[![") for ln in row), row
        assert lines[start + count] == ""
    # the tagline still follows the rows (not displaced)
    tail = 2 + len(BADGES_GITHUB) + 1 + len(BADGES_PACKAGE) + 1
    assert lines[tail].startswith("Zero-touch session rotation")


def test_badge_lines_are_exact():
    lines = TEXT.splitlines()
    assert lines[2:2 + len(BADGES_GITHUB)] == BADGES_GITHUB
    start = 2 + len(BADGES_GITHUB) + 1
    assert lines[start:start + len(BADGES_PACKAGE)] == BADGES_PACKAGE


def test_live_counters_hit_the_repo():
    for line in BADGES_GITHUB[:4]:
        assert "https://img.shields.io/github/" in line
        assert "pqant/claude-continuity" in line
    for line in (BADGES_PACKAGE[0], BADGES_PACKAGE[1], BADGES_PACKAGE[4]):
        assert "img.shields.io/pypi/" in line
        assert "cc-continuity" in line


def test_static_badges_link_into_community_files():
    assert "(CODE_OF_CONDUCT.md)" in TEXT
    assert "(CONTRIBUTING.md)" in TEXT
    assert "(SECURITY.md)" in TEXT


def test_pyproject_declares_classifiers_for_the_python_badge():
    # The PYTHON badge reads the published metadata; without these
    # classifiers it renders "missing" (issue #74).
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for want in [
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Development Status :: 5 - Production/Stable",
        "License :: OSI Approved :: MIT License",
    ]:
        assert want in pyproject, want
