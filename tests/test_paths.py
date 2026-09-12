"""paths.py: state layout under ~/.claude-continuity (PRD §14) with CC_CONTINUITY_HOME override."""

from cc_continuity import paths


def test_default_home_is_dot_claude_continuity(monkeypatch):
    monkeypatch.delenv("CC_CONTINUITY_HOME", raising=False)
    assert paths.continuity_home() == paths.Path.home() / ".claude-continuity"


def test_env_override_wins(cc_home):
    assert paths.continuity_home() == cc_home


def test_env_override_read_per_call(cc_home, tmp_path, monkeypatch):
    assert paths.continuity_home() == cc_home
    other = tmp_path / "cc-home-2"
    monkeypatch.setenv("CC_CONTINUITY_HOME", str(other))
    assert paths.continuity_home() == other  # no module-level caching


def test_ensure_root_creates_layout(cc_home):
    root = paths.ensure_root()
    assert root == cc_home
    for sub in ("runtime", "logs", "projects"):
        assert (cc_home / sub).is_dir()


def test_runtime_logs_projects_dirs_follow_override(cc_home):
    paths.ensure_root()
    assert paths.runtime_dir() == cc_home / "runtime"
    assert paths.logs_dir() == cc_home / "logs"
    assert paths.projects_dir() == cc_home / "projects"
    assert paths.config_path() == cc_home / "config.json"


def test_project_and_session_dirs(cc_home):
    key = "proj-1a2b3c4d"
    assert paths.project_dir(key) == cc_home / "projects" / key
    assert paths.session_dir(key, 17) == cc_home / "projects" / key / "sessions" / "017"
    assert paths.session_dir(key, 1234) == cc_home / "projects" / key / "sessions" / "1234"
