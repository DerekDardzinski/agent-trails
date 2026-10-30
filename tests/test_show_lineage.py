"""Tests for the show and lineage operations against real Dolt."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from trails.errors import RecordNotFoundError, ValidationError
from trails.models import (
    ArtifactRef,
    CodeCapture,
    Link,
    ProjectFramingDraft,
    ProjectPath,
    RecordDraft,
)
from trails.ops import context
from trails.ops.lineage import lineage
from trails.ops.show import show
from trails.store import Store


def _call(store: Store, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _write(store: Store, title: str, body: str = "", **fields: Any) -> str:
    fields.setdefault("entry_type", "note")
    fields.setdefault("work_status", "closed")
    project = fields.pop("project", None)
    if project is not None:
        registered = (
            store.get_project(project) or store.write_project(project, "host", [], []).project
        )
        fields["projects"] = [registered.id]
    return store.write_record(RecordDraft(title=title, body=body, **fields)).record.id


BODY = "\n".join(f"line {n}" for n in range(1, 16))


def test_summary_truncates_at_12_lines_and_lists_an_incoming_contradiction(store: Store) -> None:
    """Catches the head cut off by one, the remaining-line count wrong, and an incoming link
    dropped, which would hide that another record disputes this one."""
    claim = _write(store, "Claim", BODY, entry_type="result")
    rebuttal = _write(
        store, "Rebuttal", entry_type="note", links=[Link(relation="contradicts", target_id=claim)]
    )

    lines = _call(store, show, item_id=claim).render_text().splitlines()

    assert lines[0] == f"state: {claim} · lifecycle current · work closed · detail summary"
    assert lines[4:17] == [*(f"line {n}" for n in range(1, 13)), "(3 more lines)"]
    assert f"  contradicts  {rebuttal}  note  Rebuttal  current · work closed" in lines
    assert lines[-1] == (
        f'Next: 1 contradict this · search(links="contradicts:{claim}") · '
        f'show("{claim}", detail="full") · show("{claim}", detail="provenance") · '
        f'lineage("{claim}")'
    )


def test_full_and_provenance_show_what_summary_leaves_out(store: Store) -> None:
    """Catches full truncating the body, and provenance omitting the git command or the
    structured value."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    record_id = _write(
        store,
        "Result",
        BODY,
        value={"lift": 0.02},
        code=[CodeCapture(repo_path="/tmp/gone", head_sha=None, captured_sha=sha, dirty=True)],
    )

    full = _call(store, show, item_id=record_id, detail="full").render_text().splitlines()
    provenance = _call(store, show, item_id=record_id, detail="provenance").render_text()

    assert "line 15" in full
    assert full[-1] == (
        f'Next: show("{record_id}") · show("{record_id}", detail="provenance") · '
        f'lineage("{record_id}")'
    )
    store_dir = store.db_path / "captures.git"
    assert f"    read files: git --git-dir={store_dir} show {sha}:<path>" in provenance.splitlines()
    assert f"  /tmp/gone @ {sha} · absent" in provenance.splitlines()
    assert 'value: {"lift": 0.02}' in provenance.splitlines()


def test_lineage_follows_builds_on_and_supersedes_both_ways_and_nothing_else(
    store: Store,
) -> None:
    """Catches the walk following the wrong direction, a contradicts link, or losing the
    step count past the first link."""
    base = _write(store, "Hypothesis", entry_type="hypothesis")
    middle = _write(
        store, "Result", entry_type="result", links=[Link(relation="builds_on", target_id=base)]
    )
    check = _write(
        store,
        "Check",
        entry_type="verification",
        links=[Link(relation="builds_on", target_id=middle)],
    )
    revised = _write(store, "Check v2", entry_type="verification", supersedes=check, reason="redo")
    _write(store, "Dispute", links=[Link(relation="contradicts", target_id=middle)])

    result = _call(store, lineage, record_id=middle)

    assert [(r.relation, r.record.id, r.depth) for r in result.ancestors] == [
        ("builds_on", base, 1)
    ]
    assert [(r.relation, r.record.id, r.depth) for r in result.descendants] == [
        ("builds_on", check, 1),
        ("supersedes", revised, 2),
    ]
    lines = result.render_text().splitlines()
    assert lines[0] == f"state: {middle} · 1 ancestors · 2 descendants · up to 5 steps"
    assert lines[-1] == f'Next: show("{base}")'


def test_lineage_of_an_unlinked_record_suggests_a_search_by_title(store: Store) -> None:
    """Catches a lineage response with no Next: line when nothing is related."""
    alone = _write(store, "Lonely fact")

    lines = _call(store, lineage, record_id=alone).render_text().splitlines()

    assert lines[-1] == 'Next: search(query="Lonely fact")'


def test_lineage_stops_at_five_steps(store: Store) -> None:
    """Catches the depth limit drifting: from the end of a 7-record builds_on chain, exactly
    the 5 nearest ancestors are reported."""
    chain = [_write(store, "Step 0")]
    for n in range(1, 7):
        chain.append(
            _write(store, f"Step {n}", links=[Link(relation="builds_on", target_id=chain[-1])])
        )

    result = _call(store, lineage, record_id=chain[-1])

    assert [(r.record.id, r.depth) for r in result.ancestors] == [
        (chain[5], 1),
        (chain[4], 2),
        (chain[3], 3),
        (chain[2], 4),
        (chain[1], 5),
    ]


def test_provenance_distinguishes_unscoped_from_a_registered_project_without_paths(
    store: Store,
) -> None:
    """Catches unscoped records being rendered as though they belonged to a project."""
    loose = _write(store, "Loose")
    named = _write(store, "Named", project="ghost")

    def project_line(record_id: str) -> str:
        text = _call(store, show, item_id=record_id, detail="provenance").render_text()
        return next(line for line in text.splitlines() if line.startswith("project paths:"))

    assert project_line(loose) == "project paths: none, the record is unscoped"
    assert project_line(named) == "project paths: none"


def test_summary_checks_existence_and_provenance_compares_digests_and_lists_paths(
    store: Store, tmp_path: Path
) -> None:
    """labbook-79y.10 criterion 7 and section 5. Catches the summary hashing every artifact
    (it reports a modified file as changed), provenance not comparing, a directory artifact
    reported absent, and the project's registered paths missing from provenance."""
    data = tmp_path / "scores.csv"
    data.write_text("a,b\n1,3\n")
    folder = tmp_path / "outputs"
    folder.mkdir()
    registered = store.write_project(
        "mint",
        "h",
        [
            ProjectPath(path=str(tmp_path), label="analysis code"),
            ProjectPath(path="s3://bucket/mint/", access_instructions="aws --profile ro"),
        ],
        [],
    )
    record_id = _write(
        store,
        "Result",
        project="mint",
        artifacts=[
            ArtifactRef(location=str(data), digest="sha256:" + "ab" * 32),
            ArtifactRef(location=str(folder)),
        ],
    )

    summary = _call(store, show, item_id=record_id).render_text().splitlines()
    provenance = _call(store, show, item_id=record_id, detail="provenance").render_text()

    assert (
        f"  reachable  {data} · {data} exists; its digest is compared by show detail "
        "provenance and by lint" in summary
    )
    assert f"  referenced  {folder} · {folder} is a directory; referenced, not checked" in summary
    lines = provenance.splitlines()
    assert any(line.startswith(f"  changed  {data} · ") for line in lines)
    start = lines.index("project paths:")
    assert lines[start + 1 : start + 3] == [
        f"  {registered.project.id} (mint) · {tmp_path}  analysis code · exists",
        f"  {registered.project.id} (mint) · s3://bucket/mint/ · access: aws --profile ro "
        "· not checked",
    ]


def test_provenance_prints_the_diff_command_for_a_dirty_capture(store: Store) -> None:
    """labbook-79y.10. Catches a dirty capture with no way to read what was uncommitted: the
    capture commit has no parent, so only the repository plus the store's objects can diff it."""
    head, sha = "1" * 40, "2" * 40
    record_id = _write(
        store,
        "Result",
        code=[CodeCapture(repo_path="/r", head_sha=head, captured_sha=sha, dirty=True)],
    )

    text = _call(store, show, item_id=record_id, detail="provenance").render_text()

    objects = store.db_path / "captures.git" / "objects"
    assert (
        f"    uncommitted changes, while /r exists: GIT_ALTERNATE_OBJECT_DIRECTORIES={objects} "
        f"git -C /r diff {head} {sha}"
    ) in text.splitlines()


def test_show_array_preserves_first_occurrence_order_and_labels_missing_ids(store: Store) -> None:
    """Catches a partial miss failing found records, database order replacing request order,
    duplicate ids spending context twice, or one Next line per record."""
    first = _write(store, "First", "first body")
    second = _write(store, "Second", "second body")
    missing = "rec-0000000000000000"

    result = _call(store, show, item_id=[second, missing, first, second], detail="full")
    text = result.render_text()

    assert result.requested == [second, missing, first]
    assert [item.record.id for item in result.records] == [second, first]
    assert result.missing == [missing]
    assert text.splitlines()[0] == (
        "state: 2 records and 0 projects found · 1 not found · detail full"
    )
    assert text.splitlines()[1] == f"not found: {missing}"
    assert text.index("second body") < text.index("first body")
    assert text.count("Next:") == 1


def test_show_missing_single_id_stays_an_error_and_empty_array_is_refused(store: Store) -> None:
    """Catches array partial-success semantics weakening a missing single lookup, or an empty
    list returning a plausible success with no records."""
    missing = "rec-0000000000000000"

    with pytest.raises(RecordNotFoundError, match=f"no record or project has id '{missing}'"):
        _call(store, show, item_id=missing)
    with pytest.raises(ValidationError, match="at least one record or project id"):
        _call(store, show, item_id=[])


def test_show_separates_lifecycle_from_work_status_and_full_shows_history(store: Store) -> None:
    """Catches work status replacing lifecycle or detailed output dropping transition reasons."""
    record_id = _write(store, "Testable claim", entry_type="hypothesis", work_status="open")
    store.set_work_status(record_id, "closed", "planned analysis completed")

    summary = _call(store, show, item_id=record_id).render_text()
    full = _call(store, show, item_id=record_id, detail="full").render_text()

    assert summary.splitlines()[0] == (
        f"state: {record_id} · lifecycle current · work closed · detail summary"
    )
    assert "work status history:" not in summary
    assert "work status history:" in full
    assert "  0  open ·" in full
    assert "  1  closed ·" in full
    assert "planned analysis completed" in full


def test_show_mixes_record_and_project_ids_and_full_lists_framing_history(store: Store) -> None:
    project = store.write_project(
        "Mixed",
        "host",
        [],
        [],
        framing=ProjectFramingDraft(title="First frame", body="First purpose."),
    )
    store.write_project(
        "Mixed",
        "host",
        [],
        [],
        framing=ProjectFramingDraft(title="Second frame", body="Second purpose."),
        base_version=1,
        reason="Clarified.",
    )
    record = _write(store, "Record", "Record body", projects=[project.project.id])

    result = _call(store, show, item_id=[project.project.id, record], detail="full")
    text = result.render_text()

    assert result.requested == [project.project.id, record]
    assert [item.record.id for item in result.records] == [record]
    assert [item.project.id for item in result.projects] == [project.project.id]
    assert [item.version for item in result.projects[0].framing_history] == [2, 1]
    assert text.index("Second purpose.") < text.index("Record body")


def test_show_reports_a_procedure_never_verified_and_a_stamped_one_with_its_date(
    store: Store,
) -> None:
    """Catches an unstamped procedure reading as fine: with prime no longer listing records,
    show and search are where an agent learns a procedure was never run."""
    never = _write(store, "Run the thing", entry_type="procedure", work_status="open")
    stamped = _write(store, "Run the other thing", entry_type="procedure", work_status="open")
    store.stamp_verified(stamped, "green", date(2026, 10, 1))

    never_text = _call(store, show, item_id=never).render_text()
    stamped_text = _call(store, show, item_id=stamped).render_text()

    assert "never verified, run it and then mark_verified" in never_text
    assert "verified 2026-10-01 in green" in stamped_text
    assert "never verified" not in stamped_text
