"""validate.py: PRD §12 — checkpoint vs current project comparison."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from cc_continuity import checkpoint, config, paths, state, validate
from hookutil import make_repo, register_session, run_hook, payload

_ID = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
}
_GIT_ENV = {**os.environ, **_ID}

HEADING_ORDER = [
    "Objective", "Current Task and Exact Stopping Point", "Completed This Session",
    "Next Atomic Action", "Remaining Plan", "Decisions and Rationale",
    "Rejected Approaches — Do Not Repeat",
    "Open Problems, Risks, Questions for the User", "Runtime Environment Notes",
]
BODIES = {
    "Objective": "Ship the checkpoint engine for cc-continuity this session.",
    "Current Task and Exact Stopping Point": (
        "Stopped after implementing finalize; tests green."),
    "Completed This Session": "- Wrote finalize\n- Wrote crash tests",
    "Next Atomic Action": "PLACEHOLDER",   # replaced per test via naa_token
    "Remaining Plan": "1. CLI",
    "Decisions and Rationale": "Fault injection for step tests.",
    "Rejected Approaches — Do Not Repeat": "Rejected model-written snapshots.",
    "Open Problems, Risks, Questions for the User": "Watch the gitsnap flake.",
    "Runtime Environment Notes": "Local vLLM, effort low.",
}


def _draft(naa_token: str) -> str:
    bodies = dict(BODIES)
    bodies["Next Atomic Action"] = f"Edit `{naa_token}` and run the quick check."
    return "\n".join(f"## {t}\n{bodies[t]}\n" for t in HEADING_ORDER)


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _checkpointed(tmp_path, naa_token="a.txt"):
    """A repo with one committed file and one VERIFIED checkpoint."""
    repo = make_repo(tmp_path)                 # commits a.txt
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft(naa_token), encoding="utf-8")
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo), root=repo)
    return repo, key, nnn


def _cp_dir(key, nnn):
    return paths.session_dir(key, nnn) / "checkpoint"


def test_validate_ok_unchanged(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "OK"
    assert "working tree unchanged" in " ".join(details)


def test_validate_tree_changed_lists_paths(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    (repo / "b.txt").write_text("b\n")
    (repo / "a.txt").write_text("changed\n")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    joined = "\n".join(details)
    assert "b.txt" in joined and "a.txt" in joined


def test_validate_head_fast_forwarded_is_note(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "post-checkpoint commit")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "OK"
    assert any("fast-forwarded" in d for d in details)


def test_validate_head_rewound_mismatch(cc_home, tmp_path):
    # one checkpoint per session: commit "second" BEFORE the single finalize
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "second")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft("a.txt"), encoding="utf-8")
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo), root=repo)
    _git(repo, "reset", "--hard", "HEAD^")      # rewind past the cp head
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    assert any("rewound" in d for d in details)


def test_validate_head_diverged_mismatch(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "second")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft("a.txt"), encoding="utf-8")
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo), root=repo)
    _git(repo, "reset", "--hard", "HEAD^")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "divergent")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    assert any("diverged" in d for d in details)


def test_validate_branch_changed_mismatch(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    _git(repo, "checkout", "-qb", "other")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    assert any("branch changed" in d for d in details)


def test_validate_naa_file_to_be_created_is_exempt(cc_home, tmp_path):
    # Gate 5 finding A: mid-task rotation — NAA names the file the next
    # action will CREATE. Absent at snapshot time and at claim time; the
    # checkpoint must still validate OK.
    repo, key, nnn = _checkpointed(tmp_path, naa_token="shop/models.py")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "OK", details


def test_validate_next_atomic_action_file_missing(cc_home, tmp_path):
    # A token PRESENT in the snapshot tree that is deleted afterwards is a
    # destructive change: MISMATCH (the existence check must not become a
    # no-op — it still guards files the checkpoint relied on).
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (repo / "nope").mkdir()
    (repo / "nope" / "missing.py").write_text("x\n")
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _draft("nope/missing.py"), encoding="utf-8")
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo), root=repo)
    (repo / "nope" / "missing.py").unlink()
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    assert any("nope/missing.py" in d for d in details)


def test_validate_corrupted_checkpoint(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    (_cp_dir(key, nnn) / "snapshot.json").write_text("{broken", encoding="utf-8")
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=repo)
    assert status == "MISMATCH"
    assert any("unreadable" in d for d in details)


def test_validate_project_root_mismatch(cc_home, tmp_path):
    repo, key, nnn = _checkpointed(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    status, details = validate.validate_checkpoint(
        _cp_dir(key, nnn), config.load_config(cwd=repo), root=other)
    assert status == "MISMATCH"
    assert any("project mismatch" in d for d in details)


def test_validate_non_git_checkpoint(cc_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    run_hook("SessionStart", payload("SessionStart", plain, source="startup"))
    from cc_continuity import project
    key = project.project_key_for_root(project.resolve_root(plain))
    (paths.session_dir(key, 1) / "handoff.draft.md").write_text(
        _draft("f.txt"), encoding="utf-8")
    checkpoint.finalize(key, 1, cfg=config.load_config(cwd=plain), root=plain)
    status, details = validate.validate_checkpoint(
        _cp_dir(key, 1), config.load_config(cwd=plain), root=plain)
    assert status == "OK"
