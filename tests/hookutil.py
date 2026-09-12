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
from pathlib import Path

from cc_continuity import cli, paths, project

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
