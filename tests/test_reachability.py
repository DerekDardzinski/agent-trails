"""Tests for artifact and code reachability."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from trails import capture, reachability as reachability_module
from trails.models import ArtifactRef, CodeCapture
from trails.reachability import code_reachability, reachability


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_each_status_for_a_referenced_artifact(tmp_path: Path) -> None:
    """Criterion 7. Catches a modified file reported reachable, a missing one reported changed,
    and a remote location being fetched or reported absent rather than not checked."""
    data = tmp_path / "scores.csv"
    data.write_bytes(b"a,b\n1,2\n")
    recorded = _digest(b"a,b\n1,2\n")
    modified = tmp_path / "modified.csv"
    modified.write_bytes(b"a,b\n1,3\n")

    def status(location: str, digest: str | None = recorded) -> tuple[str, str | None]:
        found = reachability(ArtifactRef(location=location, digest=digest), verify=True)
        return found.status, found.path

    assert status(str(data)) == ("reachable", str(data))
    assert status(str(modified)) == ("changed", str(modified))
    assert status(str(tmp_path / "gone.csv")) == ("absent", str(tmp_path / "gone.csv"))
    assert status("s3://bucket/scores.csv") == ("unresolvable", None)
    assert status("SELECT * FROM weblab WHERE day = '2026-09-30'") == ("unresolvable", None)
    assert status(str(data), digest=None) == ("reachable", str(data))
    assert status("~3 GB of raw logs on the shared drive") == ("unresolvable", None)
    assert status("~nosuchuser_zz/x.csv") == ("unresolvable", None)
    remote = reachability(ArtifactRef(location="s3://b/k", digest=None), verify=True)
    assert remote.detail == "not checked: location is not a local path"


def test_without_verify_a_file_is_checked_for_existence_and_never_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.10 section 4: show summary does not hash. Catches the summary reading every
    byte of a large artifact, or reporting a modified file as changed when it never compared."""
    data = tmp_path / "scores.csv"
    data.write_bytes(b"a,b\n1,3\n")
    ref = ArtifactRef(location=str(data), digest=_digest(b"a,b\n1,2\n"))

    def no_hashing(path: Path) -> str:
        raise AssertionError(f"hashed {path}")

    monkeypatch.setattr(reachability_module, "_sha256", no_hashing)
    found = reachability(ref, verify=False)

    assert (found.status, found.detail) == (
        "reachable",
        f"{data} exists; its digest is compared by show detail provenance and by lint",
    )


def test_code_reachability_reads_the_capture_store_not_the_repository(
    git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path
) -> None:
    """labbook-79y.10 section 1. Catches the check run in the user's repository, where a
    capture never is, or a missing store reported reachable."""
    head = git_cmd(git_repo, "rev-parse", "HEAD")
    store = capture.open_store(tmp_path / "db")
    git_cmd(git_repo, "push", "-q", str(store), "HEAD:refs/heads/x")

    def status(where: Path, sha: str) -> str:
        item = CodeCapture(repo_path=str(git_repo), head_sha=None, captured_sha=sha, dirty=False)
        return code_reachability(item, where).status

    assert status(store, head) == "reachable"
    assert status(store, "0" * 40) == "absent"
    assert status(tmp_path / "nowhere.git", head) == "absent"


def test_a_directory_location_is_named_as_a_directory_not_absent(tmp_path: Path) -> None:
    """labbook-79y.6 item 2. Catches a directory reported as "does not exist", which sends a
    reader looking for something that is there."""
    folder = tmp_path / "outputs"
    folder.mkdir()

    found = reachability(ArtifactRef(location=str(folder), digest=None), verify=True)

    assert (found.status, found.detail) == (
        "referenced",
        f"{folder} is a directory; referenced, not checked",
    )
