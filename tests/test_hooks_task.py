"""hooks/task.py: TaskCreated/TaskCompleted ledger entries (PRD §9).

``TaskCompleted`` is the boundary signal consumed by threshold logic (Phase 5).
"""

from cc_continuity import ledger, paths

from hookutil import fixture_payload, make_repo, register_session, run_hook


def test_fixture_task_created_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("TaskCreated", fixture_payload("TaskCreated", repo))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "task_created"
    assert entry["task_id"] == "1"
    assert entry["subject"] == "S1 spike task"


def test_fixture_task_completed_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("TaskCompleted", fixture_payload("TaskCompleted", repo))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "task_completed"
    assert entry["task_id"] == "1"
    assert entry["subject"] == "S1 spike task"


def test_task_events_without_session_id_are_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = fixture_payload("TaskCreated", repo, session_id="no-such-session")
    assert run_hook("TaskCreated", p) == 0
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()


def test_mixed_task_events_keep_order(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("TaskCreated", fixture_payload("TaskCreated", repo))
    run_hook("TaskCompleted", fixture_payload("TaskCompleted", repo))
    types = [e["type"] for e in ledger.read(paths.session_dir(key, nnn))]
    assert types == ["session_start", "task_created", "task_completed"]
