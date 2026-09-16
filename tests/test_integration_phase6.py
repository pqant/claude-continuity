"""Phase 6 integration — full rotation chain WITHOUT a model.

Stop-time state (session in ROTATING with a checkpoint) + rotator.run with a
fake tmux whose /clear and paste-buffer calls run the REAL hook CLIs as
subprocesses for the new session: SessionStart(clear) claim + C.4, then
UserPromptSubmit RESUME. Asserts the complete PRD §8 chain on disk:
successor created (BOOTSTRAPPING -> ACTIVE), old session ROTATED,
checkpoint consumed, .started written, keystroke order, and that a repeated
Stop after rotation changes nothing (acceptance #9: exactly one successor).
"""

import json
import sys
import time
from pathlib import Path

from cc_continuity import (checkpoint, config, ledger, paths, project,
                           state)

from hookutil import (SID, make_repo, payload, register_session, run_hook)
from test_rotator import _DRAFT, _calls, fake_tmux  # noqa: F401

NEW_SID = "99999999-8888-7777-6666-555555555555"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _hook_cmd(event: str, payload_file: Path) -> str:
    # Subprocess form — same shape the installed hook command has.
    return (f"cd {REPO_ROOT} && "
            f"{sys.executable} -m cc_continuity hook {event} "
            f"< {payload_file}")


def _world_payloads(tmp_path: Path, repo: Path, nnn: int) -> dict:
    clear_p = tmp_path / "payload_clear.json"
    resume_p = tmp_path / "payload_resume.json"
    clear_p.write_text(json.dumps({
        "session_id": NEW_SID,
        "transcript_path": f"/tmp/fake/{NEW_SID}.jsonl",
        "cwd": str(repo),
        "hook_event_name": "SessionStart",
        "source": "clear",
    }))
    resume_p.write_text(json.dumps({
        "session_id": NEW_SID,
        "transcript_path": f"/tmp/fake/{NEW_SID}.jsonl",
        "cwd": str(repo),
        "hook_event_name": "UserPromptSubmit",
        "prompt": f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                  "apply the VALIDATION result, then continue from Next "
                  "Atomic Action. Do not repeat completed work.",
    }))
    return {"clear": clear_p, "resume": resume_p}


def _setup_rotating(cc_home, tmp_path, fake_tmux, monkeypatch,
                    resume_hook: bool = True) -> dict:
    """One repo, session 001 in ROTATING with a real checkpoint (post-Stop
    shape), and the fake tmux's side-effect hooks wired to the REAL hook
    CLIs for the new session. Returns {repo, key, nnn, rid}."""
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(_DRAFT)
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    rid = "rid-int"
    state.transition(key, nnn, "ROTATING", rotation_id=rid)
    files = _world_payloads(tmp_path, repo, nnn)
    monkeypatch.setenv("FAKE_CLEAR_HOOK", _hook_cmd("SessionStart",
                                                    files["clear"]))
    if resume_hook:
        monkeypatch.setenv("FAKE_PASTE_HOOK", _hook_cmd("UserPromptSubmit",
                                                        files["resume"]))
    else:
        monkeypatch.delenv("FAKE_PASTE_HOOK", raising=False)
    return {"repo": repo, "key": key, "nnn": nnn, "rid": rid}


def test_full_rotation_chain_via_real_hook_clis(cc_home, tmp_path, fake_tmux,
                                                 monkeypatch):
    from cc_continuity import rotator
    d = _setup_rotating(cc_home, tmp_path, fake_tmux, monkeypatch)
    cfg = dict(config.DEFAULTS)
    cfg["settle_ms"] = 0
    rc = rotator.run(key=d["key"], nnn=d["nnn"], session_id=SID, pane="%1",
                     rotation_id=d["rid"], cfg=cfg)
    assert rc == 0, (paths.logs_dir() / "events.log").read_text()

    old = state.load_state(d["key"], 1)
    assert old["state"] == "ROTATED"
    new = state.load_state(d["key"], 2)
    assert new["state"] == "ACTIVE"          # claim + RESUME in one prompt
    assert new["session_id"] == NEW_SID
    assert new["predecessor"] == "spike-repo-001"
    pref = json.loads((paths.project_dir(d["key"]) / "project.json")
                      .read_text())
    assert pref["latest_checkpoint"]["pending_consumption"] is False
    assert (paths.runtime_dir() / f"{NEW_SID}.started").is_file()
    entries = ledger.read(paths.session_dir(d["key"], 2))
    # event first, effect second (Task 4): the BOOTSTRAPPING->ACTIVE
    # transition's "state" entry lands after the user_prompt that caused it.
    assert [e["type"] for e in entries] == \
        ["session_start", "user_prompt", "state"]
    assert entries[0]["claimed"] == "spike-repo-001"
    assert entries[0]["validation"] == "OK"

    calls = _calls(fake_tmux)
    joined = [" ".join(c) for c in calls]
    idx = ["send-keys -t %1 C-u", "send-keys -t %1 -l /clear",
           "send-keys -t %1 Enter", "send-keys -t %1 -l /rename spike-repo-002",
           "paste-buffer -p -d -b ccc -t %1"]
    positions = [joined.index(s) for s in idx]
    assert positions == sorted(positions)
    buf = next(c for c in calls if c[:2] == ["set-buffer", "-b"])
    assert buf[3].startswith("CONTINUITY RESUME cp-001: read ")


def test_repeated_stop_after_rotation_changes_nothing(cc_home, tmp_path,
                                                       fake_tmux, monkeypatch):
    from cc_continuity import rotator
    d = _setup_rotating(cc_home, tmp_path, fake_tmux, monkeypatch)
    cfg = dict(config.DEFAULTS)
    cfg["settle_ms"] = 0
    assert rotator.run(key=d["key"], nnn=d["nnn"], session_id=SID, pane="%1",
                       rotation_id=d["rid"], cfg=cfg) == 0
    # The stop-hook loop keeps firing for the OLD session id:
    run_hook("Stop", payload("Stop", d["repo"], session_id=SID))
    run_hook("Stop", payload("Stop", d["repo"], session_id=SID))
    pref = json.loads((paths.project_dir(d["key"]) / "project.json")
                      .read_text())
    assert sum(1 for s in pref["sessions"] if s["session_id"] == NEW_SID) == 1
    assert state.load_state(d["key"], 2)["state"] == "ACTIVE"
    assert state.load_state(d["key"], 1)["state"] == "ROTATED"
