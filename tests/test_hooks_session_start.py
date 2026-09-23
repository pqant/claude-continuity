"""hooks/session_start.py: startup registration (PRD §9).

Register project + session (name from wrapper env or counter), create state
in BOOTSTRAPPING, append a ledger entry, inject the C.5 notice when a
checkpoint is pending consumption. Other sources (clear/resume/compact/fork)
do not register — Phases 5–6 own them.
"""

import json
import os

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


# Phase 6: "clear" now registers (P6-D3) — it is no longer a no-op source.
@pytest.mark.parametrize("source", ["resume", "compact", "fork"])
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


# ------------------------------------------------- Phase 6: clear (claim)

#: Nine-heading draft (same shape as tests/test_hooks_stop.py::_valid_draft).
_DRAFT = "\n".join(f"## {h}\n{b}\n" for h, b in [
    ("Objective", "Implement the feature end to end in the sandbox repo."),
    ("Current Task and Exact Stopping Point",
     "Just finished the edit; the test run has not happened yet."),
    ("Completed This Session", "- Added the module and its imports."),
    ("Next Atomic Action",
     "Run `pytest -q` and fix any failure that appears."),
    ("Remaining Plan", "1. Tests. 2. Docs."),
    ("Decisions and Rationale", "Kept stdlib only, per the constraint."),
    ("Rejected Approaches — Do Not Repeat", "No third-party dependencies."),
    ("Open Problems, Risks, Questions for the User",
     "None known at the moment."),
    ("Runtime Environment Notes", "No running servers or ports."),
])


def _verified_session(repo) -> tuple:
    """Session 001 in CHECKPOINT_VERIFIED with a real checkpoint (Task 2 pattern)."""
    from cc_continuity import checkpoint, config, project

    register_session(repo)  # plain startup -> session 001 (ACTIVE)
    key = project_key_for(repo)
    nnn = json.loads(
        (paths.project_dir(key) / "project.json").read_text())["counter"]
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(_DRAFT)
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    return key, nnn


def test_clear_claims_rotating_session_and_injects_c4(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = _verified_session(repo)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    assert run_hook("SessionStart", payload("SessionStart", repo,
                        session_id="sid-new-2", source="clear")) == 0
    data = _stdout_json(capsys)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("CONTINUITY BOOTSTRAP: you are spike-repo-002, "
                          "continuing spike-repo-001. VALIDATION: OK")
    assert "# Handoff cp-001" in ctx
    assert (paths.runtime_dir() / "sid-new-2.started").is_file()
    rec2 = state.load_state(key, 2)
    assert rec2["state"] == "BOOTSTRAPPING"
    assert rec2["session_id"] == "sid-new-2"
    assert state.load_state(key, 1)["state"] == "ROTATED"
    assert _project_record(repo)["latest_checkpoint"]["pending_consumption"] is False


def test_clear_claim_mismatch_still_registers_and_injects(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = _verified_session(repo)
    (repo / "a.txt").write_text("tampered\n")
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id="sid-new-3", source="clear"))
    ctx = _stdout_json(capsys)["hookSpecificOutput"]["additionalContext"]
    assert "VALIDATION: MISMATCH" in ctx
    assert state.load_state(key, 2)["state"] == "BOOTSTRAPPING"  # model must diagnose


def test_clear_without_claim_registers_active(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))  # 001 ACTIVE
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id="sid-b", source="clear"))
    assert _stdout_json(capsys) is None  # no pending cp -> no notice
    pref = _project_record(repo)
    assert pref["counter"] == 2
    rec = state.load_state(project_key_for(repo), 2)
    assert rec["state"] == "ACTIVE"
    assert rec["predecessor"] == "spike-repo-001"
    # P6-D3: the .started marker is for the rotator (claim path only) — a
    # plain clear must not leave one behind (it could unblock a rotator's
    # .started wait from an unrelated pane).
    assert not (paths.runtime_dir() / "sid-b.started").exists()


def test_clear_fresh_project_registers_plain(cc_home, capsys, tmp_path):
    # P6-D3 "clear always registers": a project with no project.json yet
    # (e.g. a session that started before the hooks existed) must register
    # normally on /clear, not fail in find_claimable.
    repo = make_repo(tmp_path)
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id="sid-fresh", source="clear"))
    assert _stdout_json(capsys) is None
    pref = _project_record(repo)
    assert pref["counter"] == 1
    rec = state.load_state(project_key_for(repo), 1)
    assert rec["state"] == "ACTIVE"
    assert rec["session_id"] == "sid-fresh"


def test_clear_without_session_id_is_noop(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    register_session(repo)  # 001 exists; the noop must not touch it
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id=None, source="clear"))
    assert capsys.readouterr().out == ""
    pref = _project_record(repo)
    assert pref["counter"] == 1
    assert len(pref["sessions"]) == 1


def test_startup_registration_pushes_session_started(cc_home, tmp_path,
                                                     monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    (cc_home / "config.json").write_text(json.dumps({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (ep, title, body, prio)) or True)
    assert run_hook("SessionStart",
                    fixture_payload("SessionStart", repo)) == 0
    assert len(sent) == 1
    ep, title, body, prio = sent[0]
    assert ep == "https://ntfy.sh/example-topic"
    assert title == "▶ cc-continuity: session started"
    assert prio == "default"
    assert body.startswith("proj=spike-repo 001 (spike-repo-001) host=")


def test_startup_registration_silent_when_notify_disabled(cc_home, tmp_path,
                                                          monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda *a, **kw: sent.append(a) or True)
    assert run_hook("SessionStart",
                    fixture_payload("SessionStart", repo)) == 0
    assert sent == []


# --- Phase 10: tmux_session field (P10-D9.1) ---------------------------------

_FAKE_TMUX_NAME = """#!/bin/sh
if [ "$1" = "display-message" ]; then
  printf '%s' "cc-spike-repo-0a9b"
fi
"""


@pytest.fixture
def fake_tmux_name(tmp_path, monkeypatch):
    """Fake tmux that answers ``display-message -p`` with a fixed session
    name. ``$TMUX`` is set (the hook no-ops without it)."""
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(_FAKE_TMUX_NAME)
    os.chmod(bin_path, 0o755)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("TMUX", "/tmp/fake,4242,0")
    return bin_path


def test_startup_records_tmux_session(cc_home, tmp_path, fake_tmux_name):
    repo = make_repo(tmp_path)
    assert run_hook("SessionStart",
                    fixture_payload("SessionStart", repo)) == 0
    key = project_key_for(repo)
    assert state.load_state(key, 1)["tmux_session"] == "cc-spike-repo-0a9b"


def test_startup_without_tmux_env_no_field(cc_home, tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    repo = make_repo(tmp_path)
    assert run_hook("SessionStart",
                    fixture_payload("SessionStart", repo)) == 0
    key = project_key_for(repo)
    assert state.load_state(key, 1).get("tmux_session") is None


def test_tmux_failure_is_swallowed(cc_home, tmp_path, monkeypatch):
    fail = tmp_path / "fail-tmux"
    fail.write_text("#!/bin/sh\nexit 3\n")
    os.chmod(fail, 0o755)
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(fail))
    monkeypatch.setenv("TMUX", "/tmp/fake,4242,0")
    repo = make_repo(tmp_path)
    # the hook must still succeed — the field is simply absent
    assert run_hook("SessionStart",
                    fixture_payload("SessionStart", repo)) == 0
    key = project_key_for(repo)
    assert state.load_state(key, 1).get("tmux_session") is None


def test_clear_claim_records_tmux_session(cc_home, tmp_path, fake_tmux_name):
    repo = make_repo(tmp_path)
    key, _nnn = _verified_session(repo)
    state.transition(key, _nnn, "ROTATING", rotation_id="r1")
    assert run_hook("SessionStart",
                    payload("SessionStart", repo, session_id="sid-new-2",
                            source="clear")) == 0
    rec2 = state.load_state(key, 2)
    assert rec2["state"] == "BOOTSTRAPPING"
    assert rec2["tmux_session"] == "cc-spike-repo-0a9b"
