"""Tests for the lint operation: each category on a store built to trigger it, and silence on a
clean one."""

import asyncio
import hashlib
import socket
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import basis
from trails import capture
from trails.errors import ValidationError
from trails.models import ArtifactRef, CodeCapture, ProjectPath, RecordDraft
from trails.ops import context
from trails.ops.lint import lint
from trails.store import Store

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
MISSING_SHA = "0" * 40
ORPHAN = "rec-00000000deadbeef"


def _lint(store: Store, project: str | None = None) -> Any:
    async def go() -> Any:
        return await context.bind(lint, lambda: store.db_path)(project=project)

    return asyncio.run(go())


def _file(directory: Path, text: str) -> ArtifactRef:
    data = text.encode()
    path = directory / "out.csv"
    path.write_bytes(data)
    return ArtifactRef(location=str(path), digest=f"sha256:{hashlib.sha256(data).hexdigest()}")


def _project_id(store: Store, name: str) -> str:
    existing = store.get_project(name)
    return (
        existing.id
        if existing is not None
        else store.write_project(name, "host", [], []).project.id
    )


def _store_with(store: Store, repo: Path, git_cmd: Callable[..., str]) -> Path:
    """The database's capture store, holding `repo`'s HEAD commit."""
    captures = capture.open_store(store.db_path)
    git_cmd(repo, "push", "-q", str(captures), "HEAD:refs/heads/x")
    return captures


def _details(result: Any) -> dict[str, list[tuple[str | None, str]]]:
    return {
        name: [(item.record_id, item.detail) for item in items]
        for name, items in result.categories.items()
    }


def test_lint_reports_every_category_on_a_store_built_to_trigger_each(
    store: Store, make_git_repo: Callable[[str], Path], git_cmd: Callable[..., str]
) -> None:
    """Silent failure: a category that quietly reports nothing, so a broken artifact, an orphan
    ref, a missing project path or a secret stays in the store unnoticed; or lint writing
    while marked read-only."""
    repo = make_git_repo("project")
    captures = _store_with(store, repo, git_cmd)
    git_cmd(captures, "update-ref", f"refs/research/{ORPHAN}/0", "refs/heads/x")
    gone = repo / "no-such-dir"
    p = store.write_project(
        "p",
        "host",
        [ProjectPath(path=str(gone)), ProjectPath(path=str(repo), access_instructions=AWS_KEY)],
        [],
    ).project.id
    changed = _file(repo, "a,b\n1,2\n")
    changed_path = Path(changed.location)
    changed_path.write_text("a,b\n1,3\n")
    missing = repo / "gone.csv"

    lift = store.write_record(
        RecordDraft(
            entry_type="result",
            work_status="closed",
            title="Lift",
            tags=["lift"],
            projects=[_project_id(store, "p")],
            artifacts=[changed],
        )
    ).record
    lifts = store.write_record(
        RecordDraft(
            entry_type="result",
            work_status="closed",
            title="Lifts",
            tags=["lifts"],
            projects=[_project_id(store, "p")],
        )
    ).record
    broken = store.write_record(
        RecordDraft(
            work_status="closed",
            title="Broken",
            entry_type="note",
            projects=[_project_id(store, "p")],
            artifacts=[
                ArtifactRef(location=str(missing), digest=None),
                ArtifactRef(location="s3://bucket/key", digest=None),
                ArtifactRef(location=str(repo), digest=None),
            ],
            code=[
                CodeCapture(
                    repo_path=str(repo), head_sha=None, captured_sha=MISSING_SHA, dirty=False
                )
            ],
        )
    ).record
    never = store.write_record(
        RecordDraft(
            work_status="closed",
            title="Never",
            entry_type="procedure",
            projects=[_project_id(store, "p")],
        )
    ).record
    old = store.write_record(
        RecordDraft(
            work_status="closed",
            title="Old",
            entry_type="procedure",
            projects=[_project_id(store, "p")],
        )
    ).record
    store.stamp_verified(old.id, "green", date(2020, 1, 2))
    scratch = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="Scratch",
            body=f"aws {AWS_KEY}\n",
            tags=["scratch"],
            projects=[_project_id(store, "p")],
        )
    ).record
    store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="Elsewhere",
            tags=["scratch"],
            projects=[_project_id(store, "q")],
        )
    )

    commits = store.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"]
    tables = store.query("SELECT * FROM dolt_status")

    result = _lint(store, "p")
    ids = sorted([lift.id, lifts.id, broken.id, never.id, old.id, scratch.id])

    assert _details(result) == {
        "duplicate-tags": [(None, "lift ~ lifts")],
        "changed-or-missing-artifacts": sorted(
            [
                (
                    lift.id,
                    f"artifact[0] changed: {changed_path} exists but its sha256 is "
                    f"{hashlib.sha256(b'a,b\n1,3\n').hexdigest()}, not the recorded "
                    f"{str(changed.digest).removeprefix('sha256:')}",
                ),
                (broken.id, f"artifact[0] absent: {missing} does not exist"),
            ]
        ),
        "unreachable-code": [
            (broken.id, f"code absent: commit {MISSING_SHA} does not resolve in {captures}")
        ],
        "orphan-research-refs": [(None, f"{captures}: refs/research/{ORPHAN}/ names no record")],
        "missing-paths": [(None, f"project p: {gone} does not exist on {socket.gethostname()}")],
        "stale-procedures": sorted(
            [
                (never.id, "Never: never verified"),
                (old.id, "Old: last verified 2020-01-02 in green"),
            ]
        ),
        "unembedded": [(i, "no embedding for all-MiniLM-L6-v2/model_O4") for i in ids],
        "unembedded-framing": [],
        "possible-secrets": [
            (scratch.id, "AWSKeyDetector in body line 1"),
            (None, f"project {p} (p): AWSKeyDetector in {repo} access_instructions line 1"),
        ],
    }
    assert AWS_KEY not in result.render_text()
    # lint is marked read-only for MCP hosts: no commit and no uncommitted change.
    assert store.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"] == commits
    assert store.query("SELECT * FROM dolt_status") == tables == []


def test_lint_reports_nothing_for_a_clean_store(
    store: Store, make_git_repo: Callable[[str], Path], git_cmd: Callable[..., str]
) -> None:
    """Silent failure in the other direction: false positives an agent learns to ignore, such as
    an s3:// artifact (never checked), a directory artifact, an aliased tag pair, or a
    reachable capture."""
    repo = make_git_repo("project")
    head = git_cmd(repo, "rev-parse", "HEAD")
    captures = _store_with(store, repo, git_cmd)
    store.write_project("p", "host", [ProjectPath(path=str(repo), label="code")], [])
    store.write_tag_alias("lifts", "lift")
    records = [
        store.write_record(
            RecordDraft(
                entry_type="result",
                work_status="closed",
                title="Lift",
                tags=["lift"],
                projects=[_project_id(store, "p")],
                artifacts=[
                    _file(repo, "a,b\n1,2\n"),
                    ArtifactRef(location="s3://bucket/key", digest=None),
                    ArtifactRef(location=str(repo), digest=None),
                ],
                code=[
                    CodeCapture(repo_path=str(repo), head_sha=head, captured_sha=head, dirty=False)
                ],
            )
        ).record,
        store.write_record(
            RecordDraft(
                entry_type="result",
                work_status="closed",
                title="Lifts",
                tags=["lifts"],
                projects=[_project_id(store, "p")],
            )
        ).record,
        store.write_record(
            RecordDraft(
                work_status="closed",
                title="Sync",
                entry_type="procedure",
                projects=[_project_id(store, "p")],
            )
        ).record,
    ]
    store.stamp_verified(records[2].id, "green", datetime.now().astimezone().date())
    store.upsert_chunk_embeddings(
        [(r.id, 0, "all-MiniLM-L6-v2/model_O4", basis(n)) for n, r in enumerate(records)]
    )
    git_cmd(captures, "update-ref", f"refs/research/{records[0].id}/0", head)

    result = _lint(store, "p")

    assert {name: len(items) for name, items in result.categories.items()} == {
        "duplicate-tags": 0,
        "changed-or-missing-artifacts": 0,
        "unreachable-code": 0,
        "orphan-research-refs": 0,
        "missing-paths": 0,
        "stale-procedures": 0,
        "unembedded": 0,
        "unembedded-framing": 0,
        "possible-secrets": 0,
    }


def test_lint_skips_retired_and_superseded_procedures(store: Store) -> None:
    """labbook-79y.6 item 10. Silent failure: a procedure nobody follows any more reported as
    never verified forever, burying the current one that needs it."""
    old = store.write_record(
        RecordDraft(work_status="closed", title="Old way", entry_type="procedure")
    ).record
    store.retire(old.id, "no longer used")
    first = store.write_record(
        RecordDraft(work_status="closed", title="Sync v1", entry_type="procedure")
    ).record
    current = store.write_record(
        RecordDraft(
            work_status="closed",
            title="Sync v2",
            entry_type="procedure",
            supersedes=first.id,
            reason="step 3",
        )
    ).record

    assert _details(_lint(store))["stale-procedures"] == [(current.id, "Sync v2: never verified")]


def test_a_clean_lint_says_no_problems_and_an_unknown_project_is_named(
    store: Store, tmp_path: Path
) -> None:
    """labbook-79y.6 item 10. Silent failure: a misspelled project name reported as a clean
    project, which is the same output an agent reads as "nothing to fix"."""
    store.write_project("p", "host", [ProjectPath(path=str(tmp_path))], [])

    clean = _lint(store, "p")
    with pytest.raises(ValidationError, match="no registered project"):
        _lint(store, "P ")

    assert clean.render_text().endswith("· no problems found")
