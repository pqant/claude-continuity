"""project.py: auto-registration + counter (PRD §6.2, §13, §14)."""

import hashlib

from cc_continuity import paths, project


def _register(tmp_path, name="DemoApp"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return project.ensure_project(d), d


def test_project_key_is_deterministic_and_root_scoped():
    k1 = project.project_key_for_root("/Users/x/My Repo!")
    k2 = project.project_key_for_root("/Users/x/My Repo!")
    k3 = project.project_key_for_root("/Users/x/other-repo")
    assert k1 == k2
    assert k1 != k3
    assert k1.endswith(hashlib.sha256("/Users/x/My Repo!".encode()).hexdigest()[:8])
    assert k1.startswith("my-repo-")


def test_ensure_project_registers_once(cc_home, tmp_path):
    record, _ = _register(tmp_path)
    again, _ = _register(tmp_path)
    assert record == again  # idempotent
    assert record["slug"] == "demoapp"
    assert record["counter"] == 0
    assert record["latest_checkpoint"] is None
    assert record["sessions"] == []
    key = project.project_key_for_root(record["root"])
    assert (paths.project_dir(key) / "project.json").is_file()


def test_ensure_project_uses_git_root_when_in_repo(cc_home, tmp_path):
    import subprocess

    root = tmp_path / "GitRepo"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    record = project.ensure_project(root / "src")
    assert record["root"] == str(root.resolve())
    assert record["slug"] == "gitrepo"


def test_next_counter_increments_under_lock(cc_home, tmp_path):
    record, _ = _register(tmp_path)
    key = project.project_key_for_root(record["root"])
    assert project.next_counter(key) == 1
    assert project.next_counter(key) == 2


def test_next_counter_jumps_to_requested_and_never_backwards(cc_home, tmp_path):
    record, _ = _register(tmp_path)
    key = project.project_key_for_root(record["root"])
    assert project.next_counter(key, requested=17) == 17  # wrapper said proj-017
    assert project.next_counter(key) == 18  # continues from there
    assert project.next_counter(key, requested=5) == 19  # stale request ignored


def test_counter_persisted_across_calls(cc_home, tmp_path):
    record, _ = _register(tmp_path)
    key = project.project_key_for_root(record["root"])
    project.next_counter(key)
    # Re-read via a fresh ensure (simulates a later hook in a new process).
    again = project.ensure_project(record["root"])
    assert again["counter"] == 1
