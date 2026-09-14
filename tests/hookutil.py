"""Shared helpers for Phase 3 hook tests.

Handlers are exercised through ``cc_continuity.hooks.common.run_event`` with
the real ``cli.HOOK_HANDLERS`` registry — the same path
``cc-continuity hook <event>`` uses. ``CC_CONTINUITY_HOME`` is a temp dir
(``cc_home`` fixture, repo-root conftest) and payload ``cwd`` points at a real
temp git repo, so no test touches the developer's ``~/.claude-continuity``.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from cc_continuity import cli, paths, project, state, statusline

_GIT_ID = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
}

SID = "11111111-2222-3333-4444-555555555555"


def make_repo(tmp_path: Path, name="spike-repo") -> Path:
    """A minimal git repo to serve as the payload's ``cwd``."""
    repo = tmp_path / name
    repo.mkdir()
    env = {**os.environ, **_GIT_ID}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, env=env, check=True)
    return repo


def payload(event: str, repo: Path, session_id: str = SID, **extra) -> dict:
    """A synthetic payload with the real S1 common field names (PRD F5)."""
    base = {
        "session_id": session_id,
        "transcript_path": f"/tmp/fake/{session_id}.jsonl",
        "cwd": str(repo),
        "hook_event_name": event,
    }
    base.update(extra)
    return base


def fixture_payload(event: str, repo: Path, session_id: str = SID) -> dict:
    """A recorded S1 payload (tests/fixtures/payloads/) re-pointed at ``repo``."""
    path = Path(__file__).parent / "fixtures" / "payloads" / f"{event}.json"
    with open(path, encoding="utf-8") as f:
        p = json.load(f)["payload"]
    p["cwd"] = str(repo)
    p["session_id"] = session_id
    return p


def run_hook(event: str, p: dict) -> int:
    """Run one hook event end-to-end (common skeleton + handler registry)."""
    from cc_continuity.hooks import common

    return common.run_event(event, json.dumps(p), cli.HOOK_HANDLERS)


def project_key_for(repo: Path) -> str:
    return project.project_key_for_root(project.resolve_root(repo))


def register_session(repo: Path, session_id: str = SID) -> tuple[str, int]:
    """Run the real SessionStart hook; return ``(project_key, nnn)``."""
    run_hook("SessionStart", fixture_payload("SessionStart", repo, session_id))
    key = project_key_for(repo)
    record = json.loads((paths.project_dir(key) / "project.json").read_text())
    return key, record["sessions"][-1]["nnn"]


# ------------------------------------------------- Phase 5 shared helpers


def stdout_json(capsys) -> dict | None:
    """Parse a hook's stdout as JSON (None when the hook printed nothing)."""
    out = capsys.readouterr().out
    if not out.strip():
        return None
    return json.loads(out)


#: Transition chains from a fresh session to each state of interest (PRD §8;
#: every step is VALID_TRANSITIONS-legal from BOTH BOOTSTRAPPING and ACTIVE,
#: so the chains work before and after Task 9's initial-state change).
STATE_CHAINS = {
    "ACTIVE": ["ACTIVE"],
    "WARNED": ["ACTIVE", "WARNED"],
    "ROTATION_PENDING": ["ACTIVE", "ROTATION_PENDING"],
    "HANDOFF_REQUESTED": ["ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED"],
    "HANDOFF_FAILED": ["ACTIVE", "ROTATION_PENDING",
                       "HANDOFF_REQUESTED", "HANDOFF_FAILED"],
    "CHECKPOINT_WRITING": ["ACTIVE", "ROTATION_PENDING",
                           "HANDOFF_REQUESTED", "CHECKPOINT_WRITING"],
    "CHECKPOINT_VERIFIED": ["ACTIVE", "ROTATION_PENDING",
                            "HANDOFF_REQUESTED", "CHECKPOINT_WRITING",
                            "CHECKPOINT_VERIFIED"],
    "ROTATING": ["ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                 "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"],
    "AWAITING_MANUAL_CLEAR": ["ACTIVE", "ROTATION_PENDING",
                              "HANDOFF_REQUESTED", "CHECKPOINT_WRITING",
                              "CHECKPOINT_VERIFIED", "AWAITING_MANUAL_CLEAR"],
}


def walk_to(key: str, nnn: int, s: str) -> None:
    """Transition a registered session through STATE_CHAINS to state ``s``."""
    for step in STATE_CHAINS[s]:
        state.transition(key, nnn, step)


def set_metric(ratio: float, session_id: str = SID, stale: bool = False) -> None:
    """Write a runtime metric with the given ratio (stale: 700 s old)."""
    statusline.write_metric({
        "session_id": session_id,
        "ts": time.time() - (700 if stale else 0),
        "used_tokens": int(ratio * 262144),
        "window_tokens": 262144,
        "ratio": ratio,
    })


def draft_path_for(key: str, nnn: int) -> Path:
    return paths.session_dir(key, nnn) / "handoff.draft.md"
