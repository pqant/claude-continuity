"""hooks/session_start.py: startup registration (PRD §9).

Register project + session (name from wrapper env or counter), create state
in BOOTSTRAPPING, append a ledger entry, inject the C.5 notice when a
checkpoint is pending consumption. Other sources (clear/resume/compact/fork)
do not register — Phases 5–6 own them.
"""

import json

import pytest

from cc_continuity import ledger, paths, project, state

from hookutil import (SID, fixture_payload, make_repo, payload, project_key_for,
                      register_session, run_hook, set_metric, walk_to)


def _project_record(repo):
    key = project_key_for(repo)
    return json.loads((paths.project_dir(key) / "project.json").read_text())


def _stdout_json(capsys):
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def test_startup_registers_project_and_session(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    assert run_hook("SessionStart", fixture_payload("SessionStart", repo)) == 0
    assert _stdout_json(capsys) is None  # no pending checkpoint → no notice

    pref = _project_record(repo)
    assert pref["counter"] == 1
    assert len(pref["sessions"]) == 1
    s = pref["sessions"][0]
    assert s["name"] == "spike-repo-001"
    assert s["session_id"] == SID
    assert s["nnn"] == 1

    key = project_key_for(repo)
    st = state.load_state(key, 1)
    assert st["name"] == "spike-repo-001"
    assert st["session_id"] == SID
    assert st["state"] == "ACTIVE"  # Phase 5 D3: plain startup (no pending cp)
    assert st["predecessor"] is None

    entries = ledger.read(paths.session_dir(key, 1))
    assert len(entries) == 1
    assert entries[0]["type"] == "session_start"
    assert entries[0]["source"] == "startup"


def test_second_startup_increments_and_links_predecessor(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo, session_id=SID))
    run_hook("SessionStart",
             fixture_payload("SessionStart", repo, session_id="22222222-2222-2222-2222-222222222222"))
    key = project_key_for(repo)
    assert _project_record(repo)["counter"] == 2
    st2 = state.load_state(key, 2)
    assert st2["name"] == "spike-repo-002"
    assert st2["predecessor"] == "spike-repo-001"


def test_repeated_startup_same_sid_is_idempotent(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    p = fixture_payload("SessionStart", repo)
    run_hook("SessionStart", p)
    run_hook("SessionStart", p)
    pref = _project_record(repo)
    assert pref["counter"] == 1
    assert len(pref["sessions"]) == 1
    key = project_key_for(repo)
    assert (paths.session_dir(key, 2) / "state.json").exists() is False
    assert len(ledger.read(paths.session_dir(key, 1))) == 1  # one ledger event


@pytest.mark.parametrize("source", ["clear", "resume", "compact", "fork"])
def test_non_startup_sources_do_not_register(cc_home, tmp_path, source):
    repo = make_repo(tmp_path)
    p = fixture_payload("SessionStart", repo, session_id=SID)
    p["source"] = source
    assert run_hook("SessionStart", p) == 0
    assert not (paths.projects_dir() / project_key_for(repo) / "project.json").exists()


def test_wrapper_name_with_numeric_suffix_wins(cc_home, monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("CC_CONTINUITY_NAME", "proj-017")
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    pref = _project_record(repo)
    assert pref["counter"] == 17
    assert pref["sessions"][0]["name"] == "proj-017"
    st = state.load_state(key, 17)
    assert st["name"] == "proj-017"

    # Next counter-based name continues from 17.
    monkeypatch.delenv("CC_CONTINUITY_NAME")
    run_hook("SessionStart",
             fixture_payload("SessionStart", repo,
                             session_id="33333333-3333-3333-3333-333333333333"))
    assert state.load_state(key, 18)["name"] == "spike-repo-018"


def test_wrapper_name_without_suffix_falls_back_to_counter(cc_home, monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("CC_CONTINUITY_NAME", "auth-work")
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    assert state.load_state(key, 1)["name"] == "spike-repo-001"


def test_pending_consumption_checkpoint_injects_c5_notice(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text())
    pref["latest_checkpoint"] = {
        "cp_id": "cp-001",
        "created": "2026-09-12T05:00:00+00:00",
        "pending_consumption": True,
        "handoff_path": "/x/projects/proj/sessions/001/checkpoint/handoff.md",
    }
    pfile.write_text(json.dumps(pref))

    run_hook("SessionStart",
             fixture_payload("SessionStart", repo,
                             session_id="44444444-4444-4444-4444-444444444444"))
    out = _stdout_json(capsys)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "unconsumed checkpoint cp-001" in ctx
    assert "2026-09-12T05:00:00+00:00" in ctx
    assert "/x/projects/proj/sessions/001/checkpoint/handoff.md" in ctx
    assert ctx.startswith("CONTINUITY: an unconsumed checkpoint")


def test_consumed_checkpoint_produces_no_notice(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text())
    pref["latest_checkpoint"] = {
        "cp_id": "cp-001", "created": "2026-09-12T05:00:00+00:00",
        "pending_consumption": False,
    }
    pfile.write_text(json.dumps(pref))

    run_hook("SessionStart",
             fixture_payload("SessionStart", repo,
                             session_id="55555555-5555-5555-5555-555555555555"))
    assert _stdout_json(capsys) is None


def test_re_registered_session_still_gets_pending_notice(cc_home, capsys, tmp_path):
    """Re-fired startup for an already-registered session: no re-registration,
    but the C.5 notice must still be injected."""
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text())
    pref["latest_checkpoint"] = {
        "cp_id": "cp-001", "created": "2026-09-12T05:00:00+00:00",
        "pending_consumption": True, "handoff_path": "/x/handoff.md",
    }
    pfile.write_text(json.dumps(pref))

    run_hook("SessionStart", fixture_payload("SessionStart", repo))  # same SID again
    out = _stdout_json(capsys)
    assert out is not None
    assert "unconsumed checkpoint cp-001" in out["hookSpecificOutput"]["additionalContext"]
    assert _project_record(repo)["counter"] == 1  # still idempotent


def test_missing_cwd_is_silently_ignored(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    p = fixture_payload("SessionStart", repo)
    del p["cwd"]
    assert run_hook("SessionStart", p) == 0
    assert not (paths.projects_dir() / project_key_for(repo) / "project.json").exists()


def test_no_session_in_payload_is_silently_ignored(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    p = fixture_payload("SessionStart", repo)
    del p["session_id"]
    assert run_hook("SessionStart", p) == 0
    assert not (paths.projects_dir() / project_key_for(repo) / "project.json").exists()


# ------------------------------------------------- Phase 5 (spec D1/D3)


def test_startup_with_pending_checkpoint_still_bootstrap(cc_home, capsys,
                                                         tmp_path):
    """Guard (D3 path unchanged): pending checkpoint -> BOOTSTRAPPING + C.5."""
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))
    key = project_key_for(repo)
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text())
    pref["latest_checkpoint"] = {
        "cp_id": "cp-001", "created": "2026-09-12T05:00:00+00:00",
        "pending_consumption": True, "handoff_path": "/x/handoff.md",
    }
    pfile.write_text(json.dumps(pref))
    run_hook("SessionStart", fixture_payload("SessionStart", repo,
             session_id="44444444-4444-4444-4444-444444444444"))
    out = _stdout_json(capsys)
    assert "unconsumed checkpoint cp-001" in out["hookSpecificOutput"]["additionalContext"]
    assert state.load_state(key, 2)["state"] == "BOOTSTRAPPING"


def _run_compact(capsys, repo):
    p = fixture_payload("SessionStart", repo)
    p["source"] = "compact"
    run_hook("SessionStart", p)
    return _stdout_json(capsys)


def test_compact_in_pending_injects_state_ratio_and_boundary(cc_home, capsys,
                                                             tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    walk_to(key, nnn, "ROTATION_PENDING")
    run_hook("PostToolUse", payload("PostToolUse", repo, tool_name="Bash",
                                    tool_input={"command": "pytest -q"}))
    set_metric(0.63)
    out = _run_compact(capsys, repo)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "state ROTATION_PENDING" in ctx
    assert "context at 63%" in ctx
    assert "pytest -q" in ctx  # compact ledger_summary render
    assert "Rotation will happen at the next boundary." in ctx


def test_compact_in_active_omits_boundary_sentence(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    register_session(repo)  # Phase 5: registers in ACTIVE
    out = _run_compact(capsys, repo)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "state ACTIVE" in ctx
    assert "Rotation will happen" not in ctx


def test_compact_unregistered_is_silent(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    assert _run_compact(capsys, repo) is None
