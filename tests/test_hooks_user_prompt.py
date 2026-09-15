"""hooks/user_prompt.py: ledger entry + ``turn_active`` (PRD §9).

``{type:user_prompt, text:redact(truncate(prompt, 2000))}`` plus
``state.update_fields(turn_active=True)``. ``CONTINUITY RESUME`` handling
arrives in Phase 6.
"""

import json

from cc_continuity import ledger, paths, state

from hookutil import (fixture_payload, make_repo, payload, project_key_for,
                      register_session, run_hook)


def test_fixture_prompt_is_logged_verbatim_and_turn_becomes_active(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = fixture_payload("UserPromptSubmit", repo)
    assert run_hook("UserPromptSubmit", p) == 0

    entries = ledger.read(paths.session_dir(key, nnn))
    types = [e["type"] for e in entries]
    assert types == ["session_start", "user_prompt"]
    assert entries[1]["text"] == p["prompt"]  # 456 chars < 2000: no truncation

    st = state.load_state(key, nnn)
    assert st["turn_active"] is True


def test_prompt_truncated_to_2000_chars(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt="x" * 2500))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "user_prompt"
    assert entry["text"] == "x" * 2000


def test_prompt_is_redacted_before_logging(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit",
             payload("UserPromptSubmit", repo,
                     prompt="deploy with api_key=sk-abcdefghijklmnopqrstuvwx now"))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert "sk-abcdefghijklmnopqrstuvwx" not in entry["text"]
    assert "[REDACTED]" in entry["text"]


def test_several_prompts_append_in_order(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    for text in ("first", "second", "third"):
        run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt=text))
    entries = ledger.read(paths.session_dir(key, nnn))
    assert [e["text"] for e in entries if e["type"] == "user_prompt"] == \
        ["first", "second", "third"]
    ts = [e["ts"] for e in entries if e["type"] == "user_prompt"]
    assert ts == sorted(ts)


def test_unregistered_session_is_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = payload("UserPromptSubmit", repo, session_id="no-such-session", prompt="hi")
    assert run_hook("UserPromptSubmit", p) == 0
    # no project was registered by this event: no state dirs, no ledgers
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()


def test_missing_prompt_field_is_harmless(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo)) == 0
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry == {"type": "user_prompt", "text": "", "ts": entry["ts"]}


def test_turn_active_not_appended_to_ledger(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt="hi"))
    for e in ledger.read(paths.session_dir(key, nnn)):
        assert "turn_active" not in e


# ------------------------------------------------- Phase 6: RESUME / F14

_RESUME = ("CONTINUITY RESUME cp-001: read /x completely, apply the "
           "VALIDATION result, then continue from Next Atomic Action. "
           "Do not repeat completed work.")

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


def _project_record(repo):
    key = project_key_for(repo)
    return json.loads((paths.project_dir(key) / "project.json").read_text())


def _verified_session(repo) -> tuple:
    """Session 001 in CHECKPOINT_VERIFIED with a real checkpoint (Task 3 pattern)."""
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


def test_resume_consumes_checkpoint_and_activates(cc_home, capsys, tmp_path):
    # crash-recovery shape: 001 left a pending checkpoint (no /clear ever);
    # 002 started via startup with it (BOOTSTRAPPING + C.5); the user follows
    # the resume protocol with a RESUME prompt.
    repo = make_repo(tmp_path)
    key, nnn = _verified_session(repo)
    assert state.load_state(key, 1)["state"] == "CHECKPOINT_VERIFIED"
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id="sid-002", source="startup"))
    assert state.load_state(key, 2)["state"] == "BOOTSTRAPPING"  # pending cp
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo,
                                         session_id="sid-002",
                                         prompt=_RESUME))
    assert state.load_state(key, 2)["state"] == "ACTIVE"
    pref = _project_record(repo)
    assert pref["latest_checkpoint"]["pending_consumption"] is False
    # the BOOTSTRAPPING->ACTIVE transition's state entry lands after the
    # user_prompt entry that caused it (event first, effect second)
    entries = ledger.read(paths.session_dir(key, 2))
    types = [e["type"] for e in entries]
    assert types[-2:] == ["user_prompt", "state"]
    assert entries[-2]["text"].startswith("CONTINUITY RESUME cp-001")


def test_plain_prompt_on_bootstrapping_goes_active(cc_home, capsys, tmp_path):
    # P6-D4: assisted flow — user types free text after a manual /clear.
    repo = make_repo(tmp_path)
    key, nnn = _verified_session(repo)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    run_hook("SessionStart", payload("SessionStart", repo,
                                     session_id="sid-new", source="clear"))
    assert state.load_state(key, 2)["state"] == "BOOTSTRAPPING"
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo,
                                         session_id="sid-new",
                                         prompt="please continue"))
    assert state.load_state(key, 2)["state"] == "ACTIVE"


def test_f14_resume_from_unregistered_session_claims(cc_home, capsys, tmp_path):
    # PRD §18: SessionStart(clear) did not fire; the rotator's RESUME prompt
    # is the FIRST hook for the new session id -> it must claim itself.
    repo = make_repo(tmp_path)
    key, nnn = _verified_session(repo)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo,
                                         session_id="sid-f14",
                                         prompt=_RESUME))
    assert state.load_state(key, 1)["state"] == "ROTATED"
    rec = state.load_state(key, 2)
    assert rec["session_id"] == "sid-f14"
    assert rec["state"] == "ACTIVE"  # BOOTSTRAPPING -> ACTIVE in one prompt
    assert (paths.runtime_dir() / "sid-f14.started").is_file()
    pref = _project_record(repo)
    assert pref["latest_checkpoint"]["pending_consumption"] is False
    # + the state entry from the BOOTSTRAPPING->ACTIVE transition (event
    # first, effect second)
    entries = ledger.read(paths.session_dir(key, 2))
    assert [e["type"] for e in entries] == \
        ["session_start", "user_prompt", "state"]


def test_f14_resume_with_nothing_claimable_is_noop(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    run_hook("SessionStart", fixture_payload("SessionStart", repo))  # 001 ACTIVE
    out = run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo,
                                               session_id="sid-ghost",
                                               prompt=_RESUME))
    assert out == 0
    assert _project_record(repo)["counter"] == 1  # no successor created
