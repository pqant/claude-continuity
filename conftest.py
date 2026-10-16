"""Repo-root pytest configuration: shared fixtures for all tests.

Keeping the repo root on sys.path lets tests `import cc_continuity` without
an editable install (Phase 1 runs from the working tree).
"""

import shutil
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


#: Prerequisite binaries probed via shutil.which at call time (install,
#: doctor, launch). CI runners lack tmux and claude, which made every
#: install/doctor/uninstall test fail off the developer machine (the
#: PATH probe bypassed the injected runner). Pinning the four names
#: keeps the suite hermetic; all other names fall through to the real
#: shutil.which. Tests that need a binary to be MISSING patch
#: shutil.which inside the test body (runs after this fixture) and win.
_FIXED_PREREQ_BINS = {"tmux": "/usr/bin/tmux", "zsh": "/usr/bin/zsh",
                      "curl": "/usr/bin/curl", "claude": "/usr/bin/claude"}


@pytest.fixture(autouse=True)
def _hermetic_prereq_bins(monkeypatch):
    real = shutil.which

    def fake(name):
        return _FIXED_PREREQ_BINS.get(name, real(name))

    monkeypatch.setattr(shutil, "which", fake)
