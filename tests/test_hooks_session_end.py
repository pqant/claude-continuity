"""hooks/session_end.py: reason ledger entry + ROTATING/clear anomaly log.

PRD §9: ledger entry with reason; if reason == ``clear`` while state is
``ROTATING``, log it (successor confirmation happens at SessionStart, Phase 6).
"""

from cc_continuity import ledger, paths, state

from hookutil import fixture_payload, make_repo, payload, register_session, run_hook


def _to_rotating(key, nnn):
    for target in ("ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                   "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"):
        state.transition(key, nnn, target)


def test_fixture_session_end_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("SessionEnd", fixture_payload("SessionEnd", repo))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "session_end"
    assert entry["reason"] == "other"  # fixture's recorded reason


def test_clear_while_rotating_is_logged_to_events_log(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    _to_rotating(key, nnn)
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="clear"))
    assert ledger.read(paths.session_dir(key, nnn))[-1]["reason"] == "clear"

    events = cc_home / "logs" / "events.log"
    assert events.is_file()
    assert "ROTATING" in events.read_text()


def test_clear_while_not_rotating_is_not_logged(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert state.load_state(key, nnn)["state"] == "BOOTSTRAPPING"
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="clear"))
    assert not (cc_home / "logs" / "events.log").exists()


def test_unregistered_session_is_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = payload("SessionEnd", repo, session_id="no-such-session", reason="clear")
    assert run_hook("SessionEnd", p) == 0
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()
