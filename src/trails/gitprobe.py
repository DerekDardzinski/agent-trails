"""Git probes for code provenance.

One choice here is load-bearing and was established by probe, because its failure is
silent:

* REPO IDENTITY COMES FROM `--git-common-dir`, NEVER `--show-toplevel`. Inside a
  linked worktree `--show-toplevel` returns the worktree path, so using it makes
  ONE repository appear as SEVERAL and fragments every later retrieval by repo —
  a query then returns half its claims and looks merely unlucky.

Captured commits live in trails' own bare repository, `<db_path>/captures.git`, pinned
under `refs/research/<record_id>/<n>` where n is the repository's position in the publish
call's repos list. A pin is never moved once written.
"""

from __future__ import annotations

import fcntl
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from trails.errors import TrailsError


def repo_id(path: Path) -> str | None:
    """Return the stable identity of the repository containing `path`.

    None when `path` is not inside a git repository — research with no git
    anywhere is normal, and `code` evidence must degrade to `absent` rather than
    reject the write.
    """
    common = _git(path, "rev-parse", "--git-common-dir")
    if common is None:
        return None
    # `--git-common-dir` is relative (".git") in a normal checkout and absolute
    # inside a linked worktree, so it is resolved against the directory the
    # command ran in. resolve() also collapses the macOS /var -> /private/var
    # symlink, without which the two locations disagree on the same repository.
    return str(Path(path, common).resolve())


def pin_commit(store: Path, ref: str, sha: str) -> None:
    """Point `ref` at `sha` in the capture store so gc cannot reap it.

    THE EMPTY OLD VALUE MAKES GIT REFUSE AN EXISTING REF, so a pin is never moved: moving it
    would silently repoint another record's captured code. A failure raises rather than
    passing quietly, because an unpinned commit is one the record would describe as captured
    while gc may remove it.

    Raises:
        TrailsError: git refused the update, most likely because the ref exists.
    """
    completed = subprocess.run(
        ["git", f"--git-dir={store}", "update-ref", ref, sha, ""],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TrailsError(f"could not pin {sha} as {ref} in {store}: {completed.stderr.strip()}")


@contextmanager
def git_lock(git_dir: Path) -> Iterator[None]:
    """Hold an exclusive flock on `<git_dir>/trails.lock` for the block.

    THE ONE GIT LOCK, taken on trails' own capture store and never on a user's repository,
    so no two trails processes write the store's objects and refs at once.
    """
    git_dir.mkdir(parents=True, exist_ok=True)
    with (git_dir / "trails.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _git(path: Path, *args: str) -> str | None:
    """Run one git command in `path`; stripped stdout, or None on any failure.

    Every public function above is built on this so that "not a git repository"
    has exactly one representation.
    """
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()
