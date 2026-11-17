"""Issue #73: README badges row.

The seven shields.io badges under the title are the README's at-a-glance
vitals. Pin the exact badge lines (image URL + link target) and their
position so a later edit cannot silently drop one or retarget a link.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "README.md").read_text(encoding="utf-8")

BADGES = [
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


def test_badges_row_is_the_first_block():
    lines = TEXT.splitlines()
    assert lines[0] == "# cc-continuity"
    assert lines[1] == ""
    row = [ln for ln in lines[2:2 + 7]]
    assert all(ln.startswith("[![") for ln in row), row
    # the tagline still follows the row (not displaced)
    assert lines[2 + 7] == ""
    assert lines[2 + 8].startswith("Zero-touch session rotation")


def test_badge_lines_are_exact():
    lines = TEXT.splitlines()
    assert lines[2:2 + 7] == BADGES


def test_live_counters_hit_the_repo():
    for line in BADGES[:4]:
        assert "https://img.shields.io/github/" in line
        assert "pqant/claude-continuity" in line


def test_static_badges_link_into_community_files():
    assert "(CODE_OF_CONDUCT.md)" in TEXT
    assert "(CONTRIBUTING.md)" in TEXT
    assert "(SECURITY.md)" in TEXT
