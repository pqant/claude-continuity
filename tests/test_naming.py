"""naming.py: <slug>-<NNN> rules (PRD §13) — padding, rollover, suffix continuation."""

import subprocess

import pytest

from cc_continuity import naming


# --- sanitize_slug -----------------------------------------------------------

def test_sanitize_basic():
    assert naming.sanitize_slug("My Project!") == "my-project"


def test_sanitize_already_clean_unchanged():
    assert naming.sanitize_slug("hayalet-v2") == "hayalet-v2"


def test_sanitize_special_char_runs_become_single_dash():
    assert naming.sanitize_slug("café_résumé@2x") == "caf-r-sum-2x"


def test_sanitize_truncates_to_32():
    assert naming.sanitize_slug("a" * 40) == "a" * 32
    # Truncation landing on a separator drops it.
    assert naming.sanitize_slug("a" * 31 + "-b") == "a" * 31


def test_sanitize_strips_edges():
    assert naming.sanitize_slug("--foo--") == "foo"


def test_sanitize_empty_falls_back_to_project():
    assert naming.sanitize_slug("###") == "project"
    assert naming.sanitize_slug("") == "project"


# --- pad_nnn / logical_name --------------------------------------------------

@pytest.mark.parametrize(
    "nnn,padded", [(1, "001"), (7, "007"), (99, "099"), (100, "100"),
                   (999, "999"), (1000, "1000"), (1234, "1234")]
)
def test_pad_nnn(nnn, padded):
    assert naming.pad_nnn(nnn) == padded


def test_logical_name():
    assert naming.logical_name("proj", 17) == "proj-017"


# --- split_name ---------------------------------------------------------------

def test_split_with_numeric_suffix():
    assert naming.split_name("proj-002") == ("proj", 2)
    assert naming.split_name("hayalet-v2-017") == ("hayalet-v2", 17)
    assert naming.split_name("proj-0") == ("proj", 0)


def test_split_without_numeric_suffix():
    assert naming.split_name("auth-work") == ("auth-work", None)
    assert naming.split_name("proj") == ("proj", None)
    assert naming.split_name("-007") == ("-007", None)  # empty slug part


# --- successor_name -----------------------------------------------------------

def test_successor_continues_sequence():
    assert naming.successor_name("hayalet-v2-017") == "hayalet-v2-018"
    assert naming.successor_name("proj-007") == "proj-008"


def test_successor_rollover_widens_padding():
    assert naming.successor_name("proj-099") == "proj-100"
    assert naming.successor_name("proj-999") == "proj-1000"


def test_successor_appends_001_without_suffix():
    assert naming.successor_name("auth-work") == "auth-work-001"
    assert naming.successor_name("proj") == "proj-001"


# --- slug_for_dir --------------------------------------------------------------

def test_slug_for_non_git_dir(tmp_path):
    d = tmp_path / "My Repo!"
    d.mkdir()
    assert naming.slug_for_dir(d) == "my-repo"


def test_slug_for_git_repo_uses_root_basename(tmp_path):
    root = tmp_path / "My Repo!"
    (root / "src" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    assert naming.slug_for_dir(root / "src" / "deep") == "my-repo"
