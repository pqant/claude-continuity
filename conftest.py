"""Repo-root pytest configuration: shared fixtures for all tests.

Keeping the repo root on sys.path lets tests `import cc_continuity` without
an editable install (Phase 1 runs from the working tree).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def cc_home(tmp_path, monkeypatch):
    """A temp CC_CONTINUITY_HOME so no test ever touches ~/.claude-continuity."""
    home = tmp_path / "cc-home"
    home.mkdir()
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(home))
    return home
