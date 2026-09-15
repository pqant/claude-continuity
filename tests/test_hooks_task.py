"""hooks/task.py: TaskCreated/TaskCompleted ledger entries (PRD §9).

``TaskCompleted`` is the boundary signal consumed by threshold logic (Phase 5).
"""

from cc_continuity import ledger, paths, state

from hookutil import (fixture_payload, make_repo, payload, register_session,
                      run_hook, walk_to)


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


# ------------------------------------------------- Phase 5: boundary flag

def test_task_completed_in_pending_sets_boundary_flag(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    walk_to(key, nnn, "ROTATION_PENDING")
    run_hook("TaskCompleted", fixture_payload("TaskCompleted", repo))
    rec = state.load_state(key, nnn)
    assert rec["boundary_pending"] is True
    assert rec["state"] == "ROTATION_PENDING"  # flag only — no transition
    assert ledger.read(paths.session_dir(key, nnn))[-1]["type"] == "task_completed"


def test_task_completed_in_active_leaves_flag_false(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    walk_to(key, nnn, "ACTIVE")
    run_hook("TaskCompleted", fixture_payload("TaskCompleted", repo))
    rec = state.load_state(key, nnn)
    assert rec["boundary_pending"] is False
    assert rec["state"] == "ACTIVE"


def test_task_created_in_pending_does_not_set_flag(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    walk_to(key, nnn, "ROTATION_PENDING")
    run_hook("TaskCreated", fixture_payload("TaskCreated", repo))
    assert state.load_state(key, nnn)["boundary_pending"] is False
    assert ledger.read(paths.session_dir(key, nnn))[-1]["type"] == "task_created"


def test_task_completed_subagent_payload_sets_no_flag(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    walk_to(key, nnn, "ROTATION_PENDING")
    run_hook("TaskCompleted", payload("TaskCompleted", repo, agent_id="sub-1"))
    assert state.load_state(key, nnn)["boundary_pending"] is False
    assert ledger.read(paths.session_dir(key, nnn))[-1]["type"] == "task_completed"


def test_subagent_task_entry_carries_agent_id(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("TaskCompleted", payload("TaskCompleted", repo,
                                      task_id="t1", task_subject="s",
                                      task_description="d",
                                      agent_id="agent-9"))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["agent_id"] == "agent-9"
