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


#: Env vars that can leak from the developer's shell into hook tests (tmux
#: pane, wrapper name, session id, window override, env-file export, global
#: disable). Scrubbed for every test (P6-D10); tests that need one set it
#: explicitly with monkeypatch.setenv.
_LEAKY_ENV = ("TMUX", "TMUX_PANE", "CC_CONTINUITY_NAME",
              "CC_CONTINUITY_SESSION", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
              "CLAUDE_ENV_FILE", "CC_CONTINUITY")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in _LEAKY_ENV:
        monkeypatch.delenv(var, raising=False)
