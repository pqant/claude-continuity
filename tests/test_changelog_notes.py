"""Unit tests for tools/changelog_notes.py (issue #33 release-notes extractor)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import changelog_notes as cn  # noqa: E402


SAMPLE = """\
# Changelog

All notable changes to cc-continuity are documented here.

## [Unreleased]

## [0.3.42] - 2026-11-15

### Added

- The GitHub Release is created automatically from this section (issue #33).

## [0.3.41] - 2026-11-17

### Fixed

- Zombie pane metric lands on the pane-bound session id.
"""

NOTES_42 = (
    "### Added\n\n"
    "- The GitHub Release is created automatically from this section "
    "(issue #33)."
)
NOTES_41 = (
    "### Fixed\n\n"
    "- Zombie pane metric lands on the pane-bound session id."
)


def test_extract_returns_only_the_version_section():
    assert cn.extract_notes(SAMPLE, "0.3.42") == NOTES_42


def test_extract_stops_at_the_next_section_heading():
    assert "Zombie pane metric" not in cn.extract_notes(SAMPLE, "0.3.42")


def test_extract_requires_the_exact_version_not_a_prefix():
    assert cn.extract_notes(SAMPLE, "0.3.4") is None


def test_extract_accepts_a_leading_v():
    assert cn.extract_notes(SAMPLE, "v0.3.41") == NOTES_41


def test_extract_trims_blank_edges():
    notes = cn.extract_notes(SAMPLE, "0.3.42")
    assert notes == notes.strip()


def test_extract_treats_an_empty_section_as_missing():
    # An [Unreleased] placeholder (or a section whose body was never
    # filled in) must not produce a blank release: None, so the CLI
    # fails and the workflow stops instead of publishing empty notes.
    assert cn.extract_notes(SAMPLE, "Unreleased") is None


def test_cli_writes_the_notes_to_stdout(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "changelog_notes.py"),
         "0.3.42", str(changelog)],
        capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout == NOTES_42 + "\n"


def test_cli_missing_version_exits_1(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "changelog_notes.py"),
         "9.9.9", str(changelog)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert proc.stdout == ""
