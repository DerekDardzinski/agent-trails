"""Tests for the git provenance probes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from trails.errors import TrailsError
from trails.gitprobe import pin_commit, repo_id


def test_repo_id_is_identical_from_worktree_and_parent(
    git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path
) -> None:
    """`--show-toplevel` returns the worktree path, so using it gives a linked
    worktree its own identity: one repository fragments into several and every
    later retrieval by repo silently misses half its claims."""
    worktree = tmp_path / "wt"
    git_cmd(git_repo, "worktree", "add", "-q", str(worktree), "-b", "side")

    from_parent = repo_id(git_repo)
    from_worktree = repo_id(worktree)

    assert from_parent == from_worktree
    assert from_parent is not None and from_parent.endswith("/.git")


def test_repo_id_is_none_outside_a_repo(tmp_path: Path) -> None:
    """Raising here instead of returning None would make `--code` unusable in the
    normal research setting of a workspace root that is not a repository, and the
    design requires `code` to degrade to absent rather than reject the write."""
    assert repo_id(tmp_path) is None


def _bare_copy(repo: Path, git_cmd: Callable[..., str], tmp_path: Path) -> Path:
    store = tmp_path / "store.git"
    git_cmd(tmp_path, "clone", "-q", "--bare", str(repo), str(store))
    return store


def test_pin_commit_creates_the_ref_in_the_store(
    git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path
) -> None:
    """A wrong ref or a swallowed git error leaves the commit unprotected from gc while the
    record describes it as captured."""
    store = _bare_copy(git_repo, git_cmd, tmp_path)
    sha = git_cmd(git_repo, "rev-parse", "HEAD")

    pin_commit(store, "refs/research/rec-0123456789abcdef/0", sha)

    ref = "refs/research/rec-0123456789abcdef/0"
    assert git_cmd(store, "show-ref", "--hash", ref) == sha
    assert git_cmd(git_repo, "for-each-ref", "refs/research/") == ""


def test_pin_commit_refuses_to_move_an_existing_pin(
    git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path
) -> None:
    """A pin that can be moved lets a second capture under the same id silently repoint the
    first record's code at a different commit."""
    first = git_cmd(git_repo, "rev-parse", "HEAD")
    (git_repo / "tracked.txt").write_text("changed\n")
    git_cmd(git_repo, "commit", "-qam", "second")
    store = _bare_copy(git_repo, git_cmd, tmp_path)
    second = git_cmd(git_repo, "rev-parse", "HEAD")
    ref = "refs/research/rec-0123456789abcdef/0"
    pin_commit(store, ref, first)

    with pytest.raises(TrailsError, match=ref):
        pin_commit(store, ref, second)

    assert git_cmd(store, "show-ref", "--hash", ref) == first


def test_pin_commit_raises_when_git_refuses(tmp_path: Path) -> None:
    """A pin that fails quietly leaves a record citing a commit nothing protects,
    which is the durability hole the ref exists to close."""
    with pytest.raises(TrailsError, match="refs/research"):
        pin_commit(tmp_path, "refs/research/rec-0123456789abcdef/0", "0" * 40)
