"""checkpoint.py: PRD §10 — draft validation, deterministic snapshot,
crash-safe finalization, degraded checkpoint."""

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cc_continuity import checkpoint, config, ledger, naming, paths, state
from hookutil import SID, make_repo, payload, register_session, run_hook

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
