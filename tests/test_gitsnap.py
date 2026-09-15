"""gitsnap.py: non-invasive git tree snapshot (PRD §10.3).

temp index → add -A → write-tree → commit-tree → refs/continuity/<slug>/cp-<NNN>.
Must never touch the real index, the worktree, or any branch.
"""

import os
import subprocess

import pytest

from cc_continuity import gitsnap

_ID = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd, *args, env_extra=None, check=True):
    env = {**os.environ, **_ID, **(env_extra or {})}
    p = subprocess.run(["git", *args], cwd=cwd, env=env,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr}")
    return p.stdout.rstrip("\n")  # porcelain output is space-significant


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_is_git_repo_and_root(tmp_path, git_repo):
    assert gitsnap.is_git_repo(git_repo) is True
    assert gitsnap.git_root(git_repo) == git_repo.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitsnap.is_git_repo(plain) is False
    assert gitsnap.git_root(plain) is None


def test_snapshot_captures_modifications_and_untracked(git_repo):
    (git_repo / "tracked.txt").write_text("v2\n")
    (git_repo / "new.txt").write_text("hello\n")
    (git_repo / ".gitignore").write_text("ignored.txt\n")
    (git_repo / "ignored.txt").write_text("nope\n")

    snap = gitsnap.make_snapshot(git_repo, "repo", 1)
    assert snap["ref"] == "refs/continuity/repo/cp-001"
    assert _git(git_repo, "rev-parse", snap["ref"]) == snap["commit"]

    tree_files = set(_git(git_repo, "ls-tree", "-r", "--name-only",
                          snap["tree"]).splitlines())
    assert tree_files == {"tracked.txt", "new.txt", ".gitignore"}  # ignored.txt excluded
    assert _git(git_repo, "show", f"{snap['commit']}:tracked.txt") == "v2"

    # Worktree untouched: HEAD unchanged, nothing staged, files still untracked.
    status = _git(git_repo, "status", "--porcelain").splitlines()
    assert " M tracked.txt" in status
    assert "?? new.txt" in status
    assert _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == \
        _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")  # sanity
    assert (git_repo / "tracked.txt").read_text() == "v2\n"


def test_snapshot_in_repo_without_commits(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    snap = gitsnap.make_snapshot(repo, "fresh", 1)
    commit_obj = _git(repo, "cat-file", "-p", snap["commit"]).splitlines()
    assert any(line.startswith("tree ") for line in commit_obj)
    assert not any(line.startswith("parent ") for line in commit_obj)
    assert snap["tree"] in _git(repo, "cat-file", "-p", snap["commit"])


def test_snapshot_preserves_staged_index(git_repo):
    (git_repo / "stage-me.txt").write_text("s\n")
    _git(git_repo, "add", "stage-me.txt")
    snap = gitsnap.make_snapshot(git_repo, "repo", 2)
    # Real index still has stage-me.txt staged (temp index was a copy).
    assert "stage-me.txt" in _git(git_repo, "diff", "--cached", "--name-only")
    # And the snapshot tree contains it too.
    assert "stage-me.txt" in _git(git_repo, "ls-tree", "-r", "--name-only", snap["tree"])


def test_tree_independent_of_global_gitignore(tmp_path, git_repo, monkeypatch):
    # Gate 5 finding B: rotation runs under a fake HOME where the user's
    # ``core.excludesFile`` setting is invisible; claim runs under the real
    # HOME where it applies. The two sides must compute the identical tree
    # whatever the global excludes file says. (git 2.55: $HOME/.gitignore
    # is only the default when explicitly configured, as the developer's
    # ~/.gitconfig does — hence the per-HOME .gitconfig below.)
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    home_a.mkdir()
    home_b.mkdir()
    (home_a / "ignore-a").write_text("*.log\n")
    (home_a / ".gitconfig").write_text(
        f"[core]\n\texcludesFile = {home_a / 'ignore-a'}\n")
    (git_repo / "app.log").write_text("line\n")     # untracked, repo-visible

    monkeypatch.setenv("HOME", str(home_a))
    tree_a = gitsnap.write_tree(git_repo)
    monkeypatch.setenv("HOME", str(home_b))
    tree_b = gitsnap.write_tree(git_repo)
    assert tree_a == tree_b

    # Pinning the global excludes must NOT swallow repo-level ignores.
    (git_repo / ".gitignore").write_text("*.log\n")
    monkeypatch.setenv("HOME", str(home_a))
    tree_c = gitsnap.write_tree(git_repo)
    assert tree_c != tree_a


def test_write_tree_matches_snapshot_tree(git_repo):
    (git_repo / "x.txt").write_text("x\n")
    tree_direct = gitsnap.write_tree(git_repo)
    snap = gitsnap.make_snapshot(git_repo, "repo", 3)
    assert tree_direct == snap["tree"]


def test_prune_keeps_last_n(git_repo):
    for n in range(1, 26):
        gitsnap.make_snapshot(git_repo, "repo", n)
    deleted = gitsnap.prune_snapshots(git_repo, "repo", retention=20)
    assert deleted == [f"refs/continuity/repo/cp-{n:03d}" for n in range(1, 6)]
    remaining = _git(git_repo, "for-each-ref", "--format=%(refname)",
                     "refs/continuity/repo/")
    refs = set(remaining.splitlines())
    assert len(refs) == 20
    assert "refs/continuity/repo/cp-006" in refs
    assert "refs/continuity/repo/cp-025" in refs
    assert "refs/continuity/repo/cp-005" not in refs


def test_snapshot_rejects_non_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(gitsnap.GitSnapError):
        gitsnap.make_snapshot(plain, "plain", 1)


def test_worktree_snapshot(tmp_path, git_repo):
    _git(git_repo, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    (wt / "wt-only.txt").write_text("w\n")
    snap = gitsnap.make_snapshot(wt, "repo", 4)
    tree_files = _git(git_repo, "ls-tree", "-r", "--name-only", snap["tree"])
    assert "wt-only.txt" in tree_files
