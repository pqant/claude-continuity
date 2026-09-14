"""hooks/pre_compact.py: emergency deterministic snapshot (spec §6.6).

Never blocks, prints nothing: build_snapshot (as-is, git snapshot
included) -> atomic emergency-snapshot.json. COMPACT_BEFORE_ROTATION is
logged while the state is still before CHECKPOINT_VERIFIED.
"""

import json

from cc_continuity import paths, state
from hookutil import (SID, make_repo, payload, register_session, run_hook,
                      walk_to)


def _setup(cc_home, tmp_path, s=None):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    if s:
        walk_to(key, nnn, s)
    return repo, key, nnn


def test_precompact_in_pending_writes_snapshot_and_logs(cc_home, capsys,
                                                        tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="ROTATION_PENDING")
    run_hook("PreCompact", payload("PreCompact", repo))
    assert capsys.readouterr().out == ""
    snap = json.loads((paths.session_dir(key, nnn) /
                       "emergency-snapshot.json").read_text())
    assert snap["schema_version"] == 1
    assert snap["checkpoint"] == f"cp-{nnn:03d}"
    assert snap["session"]["session_id"] == SID
    assert snap["session"]["state_at_checkpoint"] == "ROTATION_PENDING"
    assert snap["project"]["root"] == str(repo)
    assert snap["git"]["snapshot"] is not None  # real git repo -> gitsnap ref
    events = (cc_home / "logs" / "events.log").read_text()
    assert f"COMPACT_BEFORE_ROTATION state=ROTATION_PENDING nnn={nnn:03d}" in events


def test_precompact_after_checkpoint_no_log(cc_home, capsys, tmp_path):
    repo, key, nnn = _setup(cc_home, tmp_path, s="CHECKPOINT_VERIFIED")
    run_hook("PreCompact", payload("PreCompact", repo))
    assert (paths.session_dir(key, nnn) / "emergency-snapshot.json").is_file()
    assert not (cc_home / "logs" / "events.log").exists()


def test_precompact_unregistered_is_silent(cc_home, capsys, tmp_path):
    repo = make_repo(tmp_path)
    assert run_hook("PreCompact",
                    payload("PreCompact", repo, session_id="nope")) == 0
    assert capsys.readouterr().out == ""
