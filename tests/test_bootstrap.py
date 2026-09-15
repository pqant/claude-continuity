"""bootstrap.py: claim machinery (PRD §9 SessionStart, §12, §18; plan P6-D2/D3).

try_claim (caller holds the project lock): latest claimable session
(ROTATING / AMCL / CV, successor unset) -> successor record (BOOTSTRAPPING),
old -> ROTATED + successor set, checkpoint consumed, validate_checkpoint run,
C.4 text returned.
"""

import json

from cc_continuity import (bootstrap, checkpoint, config, ledger, lock,
                           paths, project, state)

from hookutil import (SID, make_repo, register_session, run_hook)

_NEW_SID = "99999999-8888-7777-6666-555555555555"


def _valid_draft() -> str:
    bodies = [
        ("Objective", "Implement the feature end to end in the sandbox repo."),
        ("Current Task and Exact Stopping Point",
         "Just finished the edit; the test run has not happened yet."),
        ("Completed This Session", "- Added the module and its imports."),
        ("Next Atomic Action", "Run `pytest -q` and fix any failure that appears."),
        ("Remaining Plan", "1. Tests. 2. Docs."),
        ("Decisions and Rationale", "Kept stdlib only, per the constraint."),
        ("Rejected Approaches — Do Not Repeat", "No third-party dependencies."),
        ("Open Problems, Risks, Questions for the User", "None known at the moment."),
        ("Runtime Environment Notes", "No running servers or ports."),
    ]
    return "\n".join(f"## {h}\n{b}\n" for h, b in bodies)


def _verified(cc_home, tmp_path) -> tuple:
    """Repo + session 001 in CHECKPOINT_VERIFIED with a REAL checkpoint."""
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert (key, nnn) == (key, 1)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(_valid_draft())
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    return repo, key, nnn


def test_c8_text_is_single_line_prd_verbatim():
    from cc_continuity import thresholds
    t = thresholds.c8_text("cp-001", "/x/checkpoint/handoff.md")
    assert "\n" not in t
    assert t == ("CONTINUITY RESUME cp-001: read /x/checkpoint/handoff.md "
                 "completely, apply the VALIDATION result, then continue from "
                 "Next Atomic Action. Do not repeat completed work.")


def test_c4_text_inlines_short_handoff():
    from cc_continuity import thresholds
    t = thresholds.c4_text(new_name="p-002", old_name="p-001", status="OK",
                           details=["working tree unchanged since checkpoint"],
                           handoff="# Handoff cp-001\n## Objective\n...",
                           handoff_path="/x/handoff.md", inline_max=12000)
    lines = t.splitlines()
    assert lines[0] == ("CONTINUITY BOOTSTRAP: you are p-002, continuing "
                        "p-001. VALIDATION: OK")
    assert lines[1] == "working tree unchanged since checkpoint"
    assert "Protocol: read the handoff below (or at /x/handoff.md) fully" in lines[2]
    assert lines[3] == "---"
    assert t.endswith("# Handoff cp-001\n## Objective\n...")


def test_c4_text_long_handoff_shows_path_only():
    from cc_continuity import thresholds
    t = thresholds.c4_text(new_name="p-002", old_name="p-001", status="OK",
                           details=[], handoff="x" * 20,
                           handoff_path="/x/handoff.md", inline_max=10)
    assert t.endswith("\n---\n/x/handoff.md")
    assert "xxxx" not in t.split("---", 1)[1]


def test_try_claim_from_rotating(cc_home, tmp_path):
    repo, key, nnn = _verified(cc_home, tmp_path)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    with lock.held(key):
        res = bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                  cfg=config.load_config(cwd=repo))
    assert res is not None
    assert res["old_name"] == "spike-repo-001" and res["old_nnn"] == 1
    assert res["new_name"] == "spike-repo-002" and res["new_nnn"] == 2
    assert res["validation"] == "OK"
    old = state.load_state(key, 1)
    assert old["state"] == "ROTATED"
    assert old["successor"] == "spike-repo-002"
    new = state.load_state(key, 2)
    assert new["state"] == "BOOTSTRAPPING"
    assert new["predecessor"] == "spike-repo-001"
    assert new["session_id"] == _NEW_SID
    assert res["bootstrap_text"].startswith(
        "CONTINUITY BOOTSTRAP: you are spike-repo-002, continuing "
        "spike-repo-001. VALIDATION: OK")
    assert "# Handoff cp-001" in res["bootstrap_text"]  # inlined
    pref = json.loads((paths.project_dir(key) / "project.json").read_text())
    assert pref["counter"] == 2
    assert pref["latest_checkpoint"]["pending_consumption"] is False
    assert pref["latest_checkpoint"]["consumed_by"] == "spike-repo-002"
    entries = ledger.read(paths.session_dir(key, 2))
    assert entries[-1]["type"] == "session_start"
    assert entries[-1]["claimed"] == "spike-repo-001"
    assert entries[-1]["validation"] == "OK"


def test_try_claim_from_checkpoints_verified_directly(cc_home, tmp_path):
    # P6-D2: manual /clear right after verification (no ROTATING ever).
    repo, key, nnn = _verified(cc_home, tmp_path)
    with lock.held(key):
        res = bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                  cfg=config.load_config(cwd=repo))
    assert res is not None and res["validation"] == "OK"
    assert state.load_state(key, 1)["state"] == "ROTATED"


def test_try_claim_from_awaiting_manual_clear(cc_home, tmp_path):
    # Assisted mode (acceptance #11): no tmux — the user types /clear after
    # the AMCL systemMessage; the claim must work from AMCL too.
    repo, key, nnn = _verified(cc_home, tmp_path)
    state.transition(key, nnn, "AWAITING_MANUAL_CLEAR")
    with lock.held(key):
        res = bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                  cfg=config.load_config(cwd=repo))
    assert res is not None and res["validation"] == "OK"
    assert state.load_state(key, 1)["state"] == "ROTATED"
    assert state.load_state(key, 2)["state"] == "BOOTSTRAPPING"


def test_try_claim_mismatch_injected_in_c4(cc_home, tmp_path):
    repo, key, nnn = _verified(cc_home, tmp_path)
    (repo / "a.txt").write_text("tampered\n")  # dirty tree, HEAD unchanged
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    with lock.held(key):
        res = bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                  cfg=config.load_config(cwd=repo))
    assert res["validation"] == "MISMATCH"
    assert "VALIDATION: MISMATCH" in res["bootstrap_text"]
    assert "working tree changed since checkpoint" in res["bootstrap_text"]


def test_try_claim_second_claim_returns_none(cc_home, tmp_path):
    # the successor marker is the double-claim guard (PRD §22.9)
    repo, key, nnn = _verified(cc_home, tmp_path)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    with lock.held(key):
        first = bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                    cfg=config.load_config(cwd=repo))
        second = bootstrap.try_claim(key, new_session_id="other-sid",
                                     cfg=config.load_config(cwd=repo))
    assert first is not None and second is None
    pref = json.loads((paths.project_dir(key) / "project.json").read_text())
    assert len(pref["sessions"]) == 2  # exactly one successor


def test_try_claim_none_when_nothing_claimable(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)  # plain ACTIVE, no checkpoint
    with lock.held(key):
        assert bootstrap.try_claim(key, new_session_id=_NEW_SID,
                                   cfg=config.load_config(cwd=repo)) is None


def test_find_claimable_missing_project_returns_none(cc_home, tmp_path):
    # A project with no project.json yet (first /clear before any
    # registration) has nothing claimable — not an error (P6-D3).
    repo = make_repo(tmp_path)
    key = project.project_key_for_root(project.resolve_root(repo))
    with lock.held(key):
        assert bootstrap.find_claimable(key) is None


def test_try_claim_long_handoff_shows_path(cc_home, tmp_path):
    repo, key, nnn = _verified(cc_home, tmp_path)
    state.transition(key, nnn, "ROTATING", rotation_id="r1")
    cfg = {**config.DEFAULTS, "inline_handoff_max_chars": 10}
    with lock.held(key):
        res = bootstrap.try_claim(key, new_session_id=_NEW_SID, cfg=cfg)
    assert res["bootstrap_text"].endswith(
        str(paths.session_dir(key, nnn) / "checkpoint" / "handoff.md"))


def test_write_started_marker(cc_home):
    p = bootstrap.write_started("sid-marker")
    assert p == paths.runtime_dir() / "sid-marker.started"
    assert p.is_file()
