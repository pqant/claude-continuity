"""checkpoint.py: PRD §10 — draft validation, deterministic snapshot,
crash-safe finalization, degraded checkpoint."""

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from cc_continuity import checkpoint, config, ledger, naming, paths, state
from hookutil import SID, make_repo, payload, register_session, run_hook

REPO_ROOT = Path(__file__).resolve().parent.parent

_ID = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
}
_GIT_ENV = {**os.environ, **_ID}


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


def _gitout(cwd, *args):
    return _git(cwd, *args).stdout.strip()


# The 9 Appendix B headings, exact strings, used by every draft fixture.
DRAFT_BODIES = {
    "Objective": "Ship the checkpoint engine for cc-continuity this session.",
    "Current Task and Exact Stopping Point": (
        "Stopped after implementing validate_draft in checkpoint.py; tests green."),
    "Completed This Session": "- Wrote validate_draft\n- Wrote 9 tests",
    "Next Atomic Action": (
        "Implement build_snapshot in `cc_continuity/checkpoint.py` and run pytest."),
    "Remaining Plan": "1. snapshot\n2. finalize\n3. CLI",
    "Decisions and Rationale": "Fault injection over real kills for step tests.",
    "Rejected Approaches — Do Not Repeat": "Rejected model-written snapshots.",
    "Open Problems, Risks, Questions for the User": (
        "gitsnap flake observed once; monitoring in Phase 4 runs."),
    "Runtime Environment Notes": "Local vLLM at 127.0.0.1:8000, effort low.",
}
HEADING_ORDER = [
    "Objective", "Current Task and Exact Stopping Point", "Completed This Session",
    "Next Atomic Action", "Remaining Plan", "Decisions and Rationale",
    "Rejected Approaches — Do Not Repeat",
    "Open Problems, Risks, Questions for the User", "Runtime Environment Notes",
]


def _draft(omit=(), **overrides):
    bodies = dict(DRAFT_BODIES, **overrides)
    parts = []
    for title in HEADING_ORDER:
        if title in omit:
            continue
        parts.append(f"## {title}\n{bodies[title]}\n")
    return "\n".join(parts)


def _cfg(**over):
    cfg = {"handoff_max_words": 2500,
           "redact_patterns": config.DEFAULTS["redact_patterns"]}
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------- draft


def test_clean_draft_ok():
    text = _draft()
    r = checkpoint.validate_draft(text, _cfg())
    assert r.ok is True and r.problems == [] and r.warnings == 0
    assert r.redacted_text == text
    assert r.word_count == len(text.split())


def test_missing_heading_reported():
    r = checkpoint.validate_draft(_draft(omit=("Remaining Plan",)), _cfg())
    assert r.ok is False
    assert "missing heading: Remaining Plan" in r.problems


def test_headings_out_of_order_reported():
    text = _draft().replace(
        "## Current Task and Exact Stopping Point", "## SENTINEL_3", 1
    ).replace("## Completed This Session",
              "## Current Task and Exact Stopping Point", 1).replace(
        "## SENTINEL_3", "## Completed This Session", 1)
    r = checkpoint.validate_draft(text, _cfg())
    assert r.ok is False
    assert any("out of order" in p for p in r.problems)


def test_duplicate_heading_reported():
    text = _draft() + "\n## Objective\nDuplicate body long enough to count.\n"
    r = checkpoint.validate_draft(text, _cfg())
    assert r.ok is False
    assert r.problems.count("duplicate heading: Objective") == 1


def test_unexpected_heading_reported():
    r = checkpoint.validate_draft(_draft() + "\n## Notes\nExtra.\n", _cfg())
    assert r.ok is False
    assert "unexpected heading: Notes" in r.problems


def test_required_section_too_short():
    r = checkpoint.validate_draft(_draft(Objective="Short."), _cfg())
    assert r.ok is False
    assert "section too short (< 20 chars): Objective" in r.problems


def test_non_required_section_may_be_empty():
    r = checkpoint.validate_draft(
        _draft(**{"Rejected Approaches — Do Not Repeat": ""}), _cfg())
    assert r.ok is True


def test_word_limit_enforced():
    long_plan = "\n".join(f"step {i}" for i in range(120))
    r = checkpoint.validate_draft(_draft(**{"Remaining Plan": long_plan}),
                                  _cfg(handoff_max_words=50))
    assert r.ok is False
    assert any(p.startswith("too long:") for p in r.problems)


def test_secrets_replaced_and_warned_not_failed():
    text = _draft(**{"Runtime Environment Notes":
                     "export token=supersecret123 and sk-" + "A" * 24 +
                     " in the shell"})
    r = checkpoint.validate_draft(text, _cfg())
    assert r.ok is True and r.warnings >= 2
    assert "[REDACTED]" in r.redacted_text
    assert "supersecret123" not in r.redacted_text
    assert "A" * 24 not in r.redacted_text


def test_hard_failure_coexists_with_warnings():
    text = _draft(omit=("Remaining Plan",),
                  **{"Runtime Environment Notes": "export token=abc123"})
    r = checkpoint.validate_draft(text, _cfg())
    assert r.ok is False
    assert r.warnings >= 1
    assert any("missing heading" in p for p in r.problems)


# -------------------------------------------------------------- git facts


def test_git_facts_dirty_repo(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("a changed\n")          # unstaged modification
    (repo / "b.txt").write_text("b\n")                   # untracked
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")                           # staged addition
    facts = checkpoint.git_facts(repo)
    assert facts["git_root"] == str(repo)
    assert facts["branch"] in ("main", "master")
    assert len(facts["head"]) == 40
    assert facts["staged"] == 1 and facts["unstaged"] == 1
    assert len(facts["status"]) == 3
    assert facts["diff_stat"]                             # a.txt in diff --stat
    assert facts["ahead"] is None and facts["behind"] is None


def test_git_facts_non_git_dir(cc_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    facts = checkpoint.git_facts(plain)
    assert facts["git_root"] is None
    assert facts["head"] is None and facts["branch"] is None
    assert facts["status"] == [] and facts["diff_stat"] == []
    assert facts["staged"] == 0 and facts["unstaged"] == 0


def test_git_facts_repo_without_commits(cc_home, tmp_path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "a.txt")
    facts = checkpoint.git_facts(repo)
    assert facts["git_root"] == str(repo)
    assert facts["head"] is None
    assert facts["branch"] == "HEAD"                      # unborn HEAD
    assert facts["staged"] == 1 and facts["unstaged"] == 0
    assert any("a.txt" in line for line in facts["status"])
    assert facts["diff_stat"]                             # staged vs empty tree


def test_git_facts_upstream_ahead_behind(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "second")
    branch = _gitout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    # a configured remote makes refs/remotes/origin/* a real remote-tracking
    # ref; without it `--set-upstream-to` refuses the bare ref
    _git(repo, "remote", "add", "origin", str(repo))
    c1 = _gitout(repo, "rev-parse", "HEAD~1")
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", c1)
    _git(repo, "branch", f"--set-upstream-to=origin/{branch}")
    facts = checkpoint.git_facts(repo)
    assert (facts["ahead"], facts["behind"]) == (1, 0)
    # diverge the upstream: different commit, same parent
    tree = _gitout(repo, "write-tree")
    c2 = _gitout(repo, "commit-tree", tree, "-p", c1, "-m", "upstream extra")
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", c2)
    facts = checkpoint.git_facts(repo)
    assert (facts["ahead"], facts["behind"]) == (1, 1)


# ------------------------------------------------------------ ledger summary

KEY = "proj-1a2b3c4d"


def _sdir():
    return paths.session_dir(KEY, 1)


def test_ledger_summary_aggregation(cc_home):
    sdir = _sdir()
    ledger.append(sdir, {"type": "tool_use", "tool": "Write", "file_path": "/x/a.py"})
    ledger.append(sdir, {"type": "tool_use", "tool": "Edit", "file_path": "/x/a.py"})
    ledger.append(sdir, {"type": "tool_use", "tool": "Bash", "command": "pytest -q"})
    ledger.append(sdir, {"type": "tool_use", "tool": "Bash",
                         "command": "boom", "interrupted": True})
    ledger.append(sdir, {"type": "tool_failure", "tool": "Bash",
                         "command": "boom", "error_first_line": "Exit code 1"})
    ledger.append(sdir, {"type": "task_created", "task_id": "1", "subject": "Build X"})
    ledger.append(sdir, {"type": "task_completed", "task_id": "1",
                         "status": "completed"})
    for i in range(12):
        ledger.append(sdir, {"type": "user_prompt", "text": f"prompt {i} " + "x" * 600})
    s = checkpoint.ledger_summary(sdir)
    assert s["files_touched"] == [{"path": "/x/a.py", "op": "Edit", "count": 2}]
    assert s["commands"] == [
        {"command": "pytest -q", "interrupted": False},
        {"command": "boom", "interrupted": True}]
    assert s["failed_commands"] == [
        {"tool": "Bash", "command": "boom", "error_first_line": "Exit code 1"}]
    assert s["tasks"] == [{"id": "1", "subject": "Build X", "status": "completed"}]
    assert len(s["user_prompts"]) == 10
    assert s["user_prompts"][0].startswith("prompt 2 ")
    assert all(len(p) == 500 for p in s["user_prompts"])


def test_ledger_summary_caps_and_order(cc_home):
    sdir = _sdir()
    for i in range(55):
        ledger.append(sdir, {"type": "tool_use", "tool": "Edit",
                             "file_path": f"/x/f{i:02d}.py"})
    for i in range(35):
        ledger.append(sdir, {"type": "tool_use", "tool": "Bash",
                             "command": f"cmd-{i}"})
    s = checkpoint.ledger_summary(sdir)
    assert len(s["files_touched"]) == 50
    assert s["files_touched"][0]["path"] == "/x/f54.py"   # most recent first
    assert s["files_touched"][0]["count"] == 1
    assert len(s["commands"]) == 30
    assert s["commands"][0] == {"command": "cmd-5", "interrupted": False}


# ---------------------------------------------------------- build snapshot

from cc_continuity import project as _project


def hookutil_key(d):
    return _project.project_key_for_root(_project.resolve_root(d))


def _registered(tmp_path, draft=None):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    if draft is not None:
        (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
            draft, encoding="utf-8")
    return repo, key, nnn


def _build(repo, key, nnn, **over):
    rec = state.load_state(key, nnn)
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text(encoding="utf-8"))
    args = dict(key=key, nnn=nnn, root=repo, cfg=config.load_config(cwd=repo),
                metric=over.pop("metric", None), degraded=False,
                created=rec["updated"], state_record=rec, project_record=pref)
    args.update(over)
    return checkpoint.build_snapshot(**args)


def test_build_snapshot_full(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path)
    (repo / "new.txt").write_text("n\n")
    snap = _build(repo, key, nnn,
                  metric={"cc_version": "2.1.209", "ratio": 0.62,
                          "window_tokens": 262144})
    assert snap["checkpoint"] == f"cp-{naming.pad_nnn(nnn)}"
    assert snap["schema_version"] == 1 and snap["degraded"] is False
    assert snap["session"]["session_id"] == SID
    assert snap["session"]["cc_version"] == "2.1.209"
    assert snap["session"]["ratio"] == 0.62
    assert snap["project"]["root"] == str(repo)
    snapref = snap["git"]["snapshot"]
    assert snapref["tree"] and len(snapref["tree"]) == 40
    assert snapref["ref"] == f"refs/continuity/{snap['project']['slug']}/cp-{naming.pad_nnn(nnn)}"
    r = subprocess.run(["git", "rev-parse", "--verify", snapref["ref"]],
                       cwd=repo, capture_output=True)
    assert r.returncode == 0                            # ref really exists
    assert any("new.txt" in line for line in snap["git"]["status"])


def test_build_snapshot_deterministic(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path)
    s1 = _build(repo, key, nnn)
    s2 = _build(repo, key, nnn)
    # same worktree → same tree sha; everything except commit shas identical
    assert s1["git"]["snapshot"]["tree"] == s2["git"]["snapshot"]["tree"]
    g1 = {k: v for k, v in s1["git"].items() if k != "snapshot"}
    g2 = {k: v for k, v in s2["git"].items() if k != "snapshot"}
    assert g1 == g2
    assert s1["files_touched"] == s2["files_touched"]
    for k in ("session", "project", "commands", "failed_commands", "tasks",
              "user_prompts"):
        assert s1[k] == s2[k], k


def test_build_snapshot_git_disabled(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path)
    (paths.config_path()).write_text(json.dumps({"git_snapshot": False}),
                                     encoding="utf-8")
    (repo / "new.txt").write_text("n\n")                  # dirty tree
    snap = _build(repo, key, nnn)
    assert snap["git"]["snapshot"] is None
    assert snap["git"]["snapshot_error"] is None
    assert snap["git"]["status"]                          # facts still collected


def test_build_snapshot_non_git_dir(cc_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    run_hook("SessionStart", payload("SessionStart", plain, source="startup"))
    key = hookutil_key(plain)
    snap = _build(plain, key, 1)
    assert snap["project"]["git_root"] is None
    assert snap["git"]["snapshot"] is None
    assert snap["git"]["status"] == []


# ---------------------------------------------------------- render handoff


def _snap(**over):
    base = {
        "schema_version": 1, "checkpoint": "cp-001",
        "created": "2026-09-13T05:00:00+00:00", "degraded": False,
        "session": {"name": "spike-001", "nnn": 1, "session_id": "sid-1",
                    "predecessor": None, "successor": None,
                    "cc_version": "2.1.209", "ratio": 0.62,
                    "window_tokens": 262144,
                    "state_at_checkpoint": "CHECKPOINT_WRITING"},
        "project": {"root": "/repo", "slug": "spike", "git_root": "/repo",
                    "branch": "main", "head": "a" * 40, "ahead": 0, "behind": 0},
        "git": {"status": [" M a.txt", "?? b.txt"], "diff_stat": [" a.txt | 1 +"],
                "staged": 0, "unstaged": 1,
                "snapshot": {"ref": "refs/continuity/spike/cp-001",
                             "commit": "b" * 40, "tree": "c" * 40},
                "snapshot_error": None},
        "files_touched": [{"path": "a.txt", "op": "Edit", "count": 2}],
        "commands": [{"command": "pytest -q", "interrupted": False}],
        "failed_commands": [{"tool": "Bash", "command": "boom",
                             "error_first_line": "Exit code 1"}],
        "tasks": [{"id": "1", "subject": "Build X", "status": "completed"}],
        "user_prompts": ["hello"],
    }
    base.update(over)
    return base


DRAFT_TEXT = _draft()


def test_render_full_draft():
    text = checkpoint.render_handoff(_snap(), DRAFT_TEXT, False)
    assert text.startswith("# Handoff cp-001 — spike-001\n")
    assert "· sid `sid-1`" in text and "2.1.209" in text
    assert "62.0%" in text and "window 262144" in text
    assert "branch `main`" in text and "a" * 7 in text
    assert "refs/continuity/spike/cp-001" in text
    assert DRAFT_TEXT.rstrip() in text          # draft verbatim
    assert "staged: 0 · unstaged: 1" in text
    assert "`a.txt` — Edit ×2" in text
    assert "hello" in text
    assert checkpoint.C7 not in text
    assert checkpoint.headings_in_order(text)   # final doc keeps the 9 in order


def test_render_degraded():
    text = checkpoint.render_handoff(_snap(degraded=True), None, True)
    assert checkpoint.C7 in text
    assert "## Objective" not in text
    assert checkpoint.headings_in_order(text) is False


def test_render_no_snapshot_clean_tree():
    s = _snap()
    s["git"] = {"status": [], "diff_stat": [], "staged": 0, "unstaged": 0,
                "snapshot": None, "snapshot_error": None}
    text = checkpoint.render_handoff(s, DRAFT_TEXT, False)
    assert "- git snapshot: —" in text
    assert "(working tree clean)" in text


def test_render_snapshot_error():
    s = _snap()
    s["git"]["snapshot"] = None
    s["git"]["snapshot_error"] = "index corrupt"
    text = checkpoint.render_handoff(s, DRAFT_TEXT, False)
    assert "- git snapshot: — (index corrupt)" in text


def test_render_no_ratio_or_predecessor():
    s = _snap()
    s["session"]["ratio"] = None
    text = checkpoint.render_handoff(s, DRAFT_TEXT, False)
    assert "ratio at rotation: —" in text
    assert "- predecessor: —" in text


# ----------------------------------------------------------------- finalize


class CrashSim(Exception):
    """Simulated kill at a fault point."""


def _fault(stop_before: int):
    def fault(i):
        if i == stop_before:
            raise CrashSim(f"killed before step {i}")
    return fault


def _cfg_for(repo):
    return config.load_config(cwd=repo)


def _pref(key):
    return json.loads((paths.project_dir(key) / "project.json")
                      .read_text(encoding="utf-8"))


def _assert_cp_valid(cp: Path):
    sums = (cp / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(sums) == 4
    for line in sums:
        digest, _, name = line.partition("  ")
        assert hashlib.sha256((cp / name).read_bytes()).hexdigest() == digest, name
    json.loads((cp / "state.json").read_text(encoding="utf-8"))
    json.loads((cp / "snapshot.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("stop", range(9))
def test_crash_at_any_step_leaves_consistent_state(cc_home, tmp_path, stop):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    with pytest.raises(CrashSim):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo,
                            fault=_fault(stop))
    sdir = paths.session_dir(key, nnn)
    cp = sdir / "checkpoint"
    st = state.load_state(key, nnn)["state"]
    assert st != "CHECKPOINT_VERIFIED"
    assert _pref(key)["latest_checkpoint"] is None
    if stop <= 1:                        # walk happens in step 1
        assert st == "BOOTSTRAPPING"
    else:
        assert st == "CHECKPOINT_WRITING"
    # tmp is written during step 5, removed only by the rename (step 7)
    tmps = list(sdir.glob("checkpoint.tmp-*"))
    assert (len(tmps) == 1) == (stop in (6, 7))
    if stop == 8:                        # renamed, but state+pointer not done
        assert cp.is_dir()
        _assert_cp_valid(cp)
    else:
        assert not cp.exists()


def test_finalize_walks_state_chain_from_bootstrap(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    with pytest.raises(CrashSim):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo,
                            fault=_fault(5))
    ev = [(e["from"], e["to"]) for e in ledger.read(paths.session_dir(key, nnn))
          if e.get("type") == "state"]
    assert ev == [("BOOTSTRAPPING", "ACTIVE"), ("ACTIVE", "ROTATION_PENDING"),
                  ("ROTATION_PENDING", "HANDOFF_REQUESTED"),
                  ("HANDOFF_REQUESTED", "CHECKPOINT_WRITING")]


def test_finalize_success_full_path(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    cp_id, degraded = checkpoint.finalize(
        key, nnn, cfg=_cfg_for(repo), root=repo,
        metric={"cc_version": "2.1.209", "ratio": 0.6, "window_tokens": 262144})
    assert cp_id == f"cp-{naming.pad_nnn(nnn)}" and degraded is False
    sdir = paths.session_dir(key, nnn)
    cp = sdir / "checkpoint"
    for name in ("handoff.md", "state.json", "snapshot.json",
                 "ledger.jsonl", "SHA256SUMS"):
        assert (cp / name).is_file(), name
    assert list(sdir.glob("checkpoint.tmp-*")) == []
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    st_cp = json.loads((cp / "state.json").read_text(encoding="utf-8"))
    assert st_cp["state"] == "CHECKPOINT_WRITING"      # copy at build time
    assert st_cp["degraded"] is False
    ptr = _pref(key)["latest_checkpoint"]
    assert ptr["cp_id"] == cp_id and ptr["nnn"] == nnn
    assert ptr["pending_consumption"] is True
    assert Path(ptr["handoff_path"]).is_file()
    _assert_cp_valid(cp)
    assert checkpoint.headings_in_order((cp / "handoff.md").read_text())
    snap = json.loads((cp / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["project"]["root"] == str(repo)
    assert (cp / "ledger.jsonl").read_text(encoding="utf-8")   # ledger copied


def test_verify_detects_tampering(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    with pytest.raises(CrashSim):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo,
                            fault=_fault(6))
    tmp = next(iter(paths.session_dir(key, nnn).glob("checkpoint.tmp-*")))
    snap = json.loads((tmp / "snapshot.json").read_text(encoding="utf-8"))
    rec = state.load_state(key, nnn)
    checkpoint._verify_checkpoint_dir(tmp, snapshot=snap, state_record=rec,
                                      root=repo, degraded=False)  # intact: ok
    handoff_bytes = (tmp / "handoff.md").read_bytes()
    (tmp / "handoff.md").write_bytes(handoff_bytes + b"tampered\n")
    with pytest.raises(checkpoint.CheckpointError, match="checksum"):
        checkpoint._verify_checkpoint_dir(tmp, snapshot=snap,
                                          state_record=rec, root=repo,
                                          degraded=False)
    (tmp / "handoff.md").write_bytes(handoff_bytes)           # restore
    ledger_bytes = (tmp / "ledger.jsonl").read_bytes()
    (tmp / "ledger.jsonl").unlink()
    with pytest.raises(checkpoint.CheckpointError, match="missing"):
        checkpoint._verify_checkpoint_dir(tmp, snapshot=snap,
                                          state_record=rec, root=repo,
                                          degraded=False)
    (tmp / "ledger.jsonl").write_bytes(ledger_bytes)  # restore for JSON phase
    bad = b"{not json"
    (tmp / "snapshot.json").write_bytes(bad)
    lines = (tmp / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    lines = [f"{hashlib.sha256(bad).hexdigest()}  snapshot.json"
             if l.endswith("snapshot.json") else l for l in lines]
    (tmp / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(checkpoint.CheckpointError, match="JSON"):
        checkpoint._verify_checkpoint_dir(tmp, snapshot=snap,
                                          state_record=rec, root=repo,
                                          degraded=False)


# ------------------------------------------------------- finalize: edges


def test_finalize_degraded_without_draft(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path)           # no draft at all
    cp_id, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo),
                                          root=repo)
    assert degraded is True
    st = state.load_state(key, nnn)
    assert st["state"] == "CHECKPOINT_VERIFIED" and st["degraded"] is True
    sdir = paths.session_dir(key, nnn)
    handoff = (sdir / "checkpoint" / "handoff.md").read_text(encoding="utf-8")
    assert checkpoint.C7 in handoff
    snap = json.loads((sdir / "checkpoint" / "snapshot.json")
                      .read_text(encoding="utf-8"))
    assert snap["degraded"] is True
    assert _pref(key)["latest_checkpoint"] is not None


def test_finalize_degraded_on_invalid_draft(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path,
                                 draft=_draft(omit=("Remaining Plan",)))
    _, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo)
    assert degraded is True
    assert state.load_state(key, nnn)["degraded"] is True


def test_finalize_from_handoff_failed_retries(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    for target in ("ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                   "HANDOFF_FAILED"):
        state.transition(key, nnn, target)
    cp_id, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo),
                                          root=repo)
    assert not degraded
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"


def test_finalize_from_checkpoint_writing_resumes(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    with pytest.raises(CrashSim):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo,
                            fault=_fault(6))         # tmp left behind
    cp_id, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo),
                                          root=repo)
    assert not degraded
    sdir = paths.session_dir(key, nnn)
    assert list(sdir.glob("checkpoint.tmp-*")) == []   # stale tmp cleaned
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"


@pytest.mark.parametrize("start", ["CHECKPOINT_VERIFIED", "ROTATING",
                                   "ROTATED", "AWAITING_MANUAL_CLEAR"])
def test_finalize_rejects_bad_states(cc_home, tmp_path, start):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    chain = {
        "CHECKPOINT_VERIFIED": ("ACTIVE", "WARNED", "ROTATION_PENDING",
                                "HANDOFF_REQUESTED", "CHECKPOINT_WRITING",
                                "CHECKPOINT_VERIFIED"),
        "ROTATING": ("ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                     "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"),
        "ROTATED": ("ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                    "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING",
                    "ROTATED"),
        "AWAITING_MANUAL_CLEAR": ("ACTIVE", "WARNED", "ROTATION_PENDING",
                                  "HANDOFF_REQUESTED", "CHECKPOINT_WRITING",
                                  "CHECKPOINT_VERIFIED",
                                  "AWAITING_MANUAL_CLEAR"),
    }[start]
    for target in chain:
        state.transition(key, nnn, target)
    with pytest.raises(checkpoint.CheckpointError, match=start):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo)
    # rejected: no tmp, no checkpoint, state untouched
    sdir = paths.session_dir(key, nnn)
    assert list(sdir.glob("checkpoint.tmp-*")) == []
    assert not (sdir / "checkpoint").exists()
    assert state.load_state(key, nnn)["state"] == start


def test_verify_failure_removes_tmp_keeps_writing_state(cc_home, tmp_path,
                                                        monkeypatch):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)

    def boom(*a, **k):
        raise checkpoint.CheckpointError("verify: simulated")

    monkeypatch.setattr(checkpoint, "_verify_checkpoint_dir", boom)
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo)
    sdir = paths.session_dir(key, nnn)
    assert list(sdir.glob("checkpoint.tmp-*")) == []
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_WRITING"
    assert _pref(key)["latest_checkpoint"] is None


def test_retry_after_crash_at_8_replaces_checkpoint(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    with pytest.raises(CrashSim):
        checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo,
                            fault=_fault(8))          # renamed, no state+ptr
    sdir = paths.session_dir(key, nnn)
    assert (sdir / "checkpoint").is_dir()
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_WRITING"
    new_draft = _draft(**{"Objective":
                          "A different objective for the second run, recorded here."})
    (sdir / "handoff.draft.md").write_text(new_draft, encoding="utf-8")
    cp_id, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo),
                                          root=repo)
    assert not degraded
    handoff = (sdir / "checkpoint" / "handoff.md").read_text(encoding="utf-8")
    assert "A different objective" in handoff
    assert list(sdir.glob("checkpoint.tmp-*")) == []
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    assert _pref(key)["latest_checkpoint"]["cp_id"] == cp_id


def test_finalize_non_git_dir(cc_home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    run_hook("SessionStart", payload("SessionStart", plain, source="startup"))
    key = hookutil_key(plain)
    (paths.session_dir(key, 1) / "handoff.draft.md").write_text(
        DRAFT_TEXT, encoding="utf-8")
    cp_id, degraded = checkpoint.finalize(key, 1, cfg=_cfg_for(plain),
                                          root=plain)
    assert not degraded
    snap = json.loads((paths.session_dir(key, 1) / "checkpoint" /
                       "snapshot.json").read_text(encoding="utf-8"))
    assert snap["project"]["git_root"] is None
    assert snap["git"]["snapshot"] is None
    assert state.load_state(key, 1)["state"] == "CHECKPOINT_VERIFIED"


# ----------------------------------------------------- real kill -9 smoke


def test_kill_during_checkpoint_never_leaves_partial_state(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    env = {**os.environ, "CC_CONTINUITY_SESSION": SID,
           "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "cc_continuity", "checkpoint"],
        cwd=repo, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.15)                    # mid-run; timing is inherently racy
    proc.kill()
    proc.wait()
    sdir = paths.session_dir(key, nnn)
    cp = sdir / "checkpoint"
    if cp.exists():                     # if present it must be FULLY valid
        _assert_cp_valid(cp)
    st = state.load_state(key, nnn)["state"]
    assert st in ("BOOTSTRAPPING", "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED")
    ptr = _pref(key)["latest_checkpoint"]
    # the pointer is written AFTER the state transition (PRD order), so the
    # pointer can never lead the state — that direction is race-free:
    if ptr is not None:
        assert st == "CHECKPOINT_VERIFIED"
        assert Path(ptr["handoff_path"]).is_file()
    elif st != "CHECKPOINT_VERIFIED":
        pass                             # expected: nothing committed
    # NOTE: a kill in the microsecond window between the state write and the
    # pointer write leaves (VERIFIED, ptr=None) — a permitted PRD-order
    # outcome, deliberately not asserted against here.
