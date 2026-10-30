"""Tests for whole-store and project-scoped prime views."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from trails.models import Link, ProjectFramingDraft, RecordDraft
from trails.ops import context
from trails.ops.prime import prime
from trails.store import Store


def _call(store: Store, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(prime, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _project(store: Store, name: str, title: str | None = None) -> str:
    framing = ProjectFramingDraft(title=title, body=f"Purpose of {name}.") if title else None
    return store.write_project(name, "host", [], [], framing=framing).project.id


def _write(store: Store, title: str, body: str = "", **fields: Any) -> str:
    fields.setdefault("entry_type", "note")
    fields.setdefault("work_status", "closed")
    return store.write_record(RecordDraft(title=title, body=body, **fields)).record.id


def test_whole_store_prime_is_a_project_index_with_unscoped_count(store: Store) -> None:
    """Catches whole prime dumping record bodies or losing a project with no framing."""
    alpha = _project(store, "Alpha", "Reduce latency")
    beta = _project(store, "Beta")
    _write(store, "Open", entry_type="hypothesis", work_status="open", projects=[alpha])
    _write(store, "Closed", projects=[alpha, beta])
    _write(store, "Loose")

    result = _call(store)
    text = result.render_text()

    assert result.project is None
    assert result.unscoped_count == 1
    assert [(item.project.id, item.record_count, item.open_count) for item in result.projects] == [
        (alpha, 2, 1),
        (beta, 1, 0),
    ]
    assert result.projects[0].framing_title == "Reduce latency"
    assert result.projects[1].framing_title is None
    assert "Purpose of Alpha" not in text
    assert f'prime(project="{alpha}")' in text


def test_whole_store_prime_pages_projects_without_hiding_total(store: Store) -> None:
    first = _project(store, "Alpha")
    second = _project(store, "Beta")

    page_one = _call(store, limit=1)
    page_two = _call(store, limit=1, offset=1)

    assert page_one.project_total == page_two.project_total == 2
    assert [item.project.id for item in page_one.projects] == [first]
    assert [item.project.id for item in page_two.projects] == [second]
    assert "prime(limit=1, offset=1)" in page_one.render_text()


def test_project_prime_shows_full_framing_and_problem_titles_without_bodies(store: Store) -> None:
    """Catches scoped prime omitting framing revision guidance, printing any record body, or
    losing the call that reads the problem statements in full."""
    project = _project(store, "Alpha", "Initial frame")
    store.write_project(
        "Alpha",
        "host",
        [],
        [],
        framing=ProjectFramingDraft(title="Current frame", body="Why Alpha exists."),
        base_version=1,
        reason="Clarified purpose.",
    )
    problem = _write(
        store,
        "Reduce variance",
        "Full objective body.",
        entry_type="problem-statement",
        work_status="open",
        projects=[project],
    )
    note = _write(
        store, "Operational note", "Do not print this body.", work_status="open", projects=[project]
    )

    result = _call(store, project="Alpha")
    text = result.render_text()

    assert result.project is not None and result.project.id == project
    assert result.framing is not None and result.framing.version == 2
    assert [record.id for record in result.problem_statements] == [problem]
    assert [entry.record.id for entry in result.groups["note"]] == [note]
    assert "Why Alpha exists." in text
    assert (
        f'  {problem}  Reduce variance · work open\n  read them: show(["{problem}"], detail="full")'
    ) in text
    assert "Full objective body." not in text
    assert "Do not print this body." not in text
    assert f'update_project(project="{project}"' in text
    assert 'base_version=2, reason="..."' in text


def test_project_prime_counts_current_replaced_and_retired_members(store: Store) -> None:
    project = _project(store, "Alpha")
    old = _write(store, "Old", projects=[project])
    _write(
        store,
        "New",
        projects=[project],
        supersedes=old,
        reason="Replaced.",
    )
    retired = _write(store, "Retired", projects=[project])
    store.retire(retired, "Wrong.")

    result = _call(store, project=project)

    assert (result.current_count, result.replaced_count, result.retired_count) == (1, 1, 1)


def test_prime_explains_result_hypothesis_and_verification_connections(store: Store) -> None:
    project = _project(store, "Alpha")
    hypothesis = _write(
        store,
        "Hypothesis",
        entry_type="hypothesis",
        work_status="open",
        projects=[project],
    )
    result_id = _write(
        store,
        "Result",
        entry_type="result",
        work_status="open",
        projects=[project],
        links=[Link(relation="builds_on", target_id=hypothesis)],
    )
    verification = _write(
        store,
        "Verification",
        entry_type="verification",
        projects=[project],
        links=[Link(relation="builds_on", target_id=result_id)],
    )

    text = _call(store, project=project).render_text()

    assert f"results built on this version: {result_id}" in text
    assert f"verified by {verification}" in text


def test_project_prime_collapses_closed_records_to_a_count_and_the_call_that_lists_them(
    store: Store,
) -> None:
    """Catches closed records listed one by one again, a count that disagrees with the records,
    or a listing call that would not return every closed record of the type."""
    project = _project(store, "Alpha")
    open_decision = _write(
        store, "Still deciding", entry_type="decision", work_status="open", projects=[project]
    )
    first = _write(store, "Settled one", entry_type="decision", projects=[project])
    second = _write(store, "Settled two", entry_type="decision", projects=[project])
    only_closed = _write(store, "Old fact", entry_type="fact", projects=[project])

    result = _call(store, project=project)
    text = result.render_text()

    assert result.closed_counts == {"decision": 2, "fact": 1}
    assert [entry.record.id for entry in result.groups["decision"]] == [open_decision]
    assert result.groups["fact"] == []
    assert (
        "decisions (3 current · 2 closed):\n"
        f"  {open_decision}  Still deciding · work open\n"
        "  2 closed, listed above only when contradicted: "
        f'search(projects=["{project}"], entry_type="decision", work_status="closed", limit=2)'
    ) in text
    assert (
        "facts (1 current · 1 closed):\n"
        "  1 closed, listed above only when contradicted: "
        f'search(projects=["{project}"], entry_type="fact", work_status="closed", limit=1)'
    ) in text
    for hidden in (first, second, only_closed):
        assert hidden not in text
    assert f'read a full record: show("{open_decision}", detail="full")' in text


def test_project_prime_counts_a_listed_contradicted_closed_record_once(store: Store) -> None:
    """Catches a closed record listed for its contradiction being counted as open as well."""
    project = _project(store, "Alpha")
    wrong = _write(store, "Wrong", entry_type="report", projects=[project])
    _write(
        store,
        "Correction",
        work_status="open",
        projects=[project],
        links=[Link(relation="contradicts", target_id=wrong)],
    )

    text = _call(store, project=project).render_text()

    assert f"reports (1 current · 1 closed):\n  {wrong}  Wrong · work closed" in text


def test_project_prime_structured_content_carries_no_record_body(store: Store) -> None:
    """Catches the 10.6 MB reply: full records, with the full records they build on, in the
    structured content an MCP host must accept in one message."""
    project = _project(store, "Alpha")
    _write(
        store,
        "Goal",
        "PROBLEM-BODY",
        entry_type="problem-statement",
        work_status="open",
        projects=[project],
    )
    hypothesis = _write(
        store,
        "Claim",
        "TARGET-BODY",
        entry_type="hypothesis",
        work_status="open",
        projects=[project],
    )
    result_id = _write(
        store,
        "Measured",
        "RESULT-BODY",
        entry_type="result",
        work_status="open",
        projects=[project],
        links=[Link(relation="builds_on", target_id=hypothesis)],
    )

    dumped = _call(store, project=project).model_dump(mode="json")

    serialized = json.dumps(dumped)
    for body in ("PROBLEM-BODY", "TARGET-BODY", "RESULT-BODY"):
        assert body not in serialized
    assert dumped["groups"]["result"][0]["builds_on"] == [
        {
            "id": hypothesis,
            "entry_type": "hypothesis",
            "title": "Claim",
            "work_status": "open",
            "superseded_by": None,
            "retired": False,
            "replaces": None,
            "last_verified": None,
        }
    ]
    assert dumped["groups"]["result"][0]["record"]["id"] == result_id


def test_whole_prime_last_activity_includes_membership_events(store: Store) -> None:
    project = _project(store, "Alpha")
    record = _write(store, "Record")
    before = _call(store).projects[0].last_activity
    store.set_projects(record, [project], [], "Grouped.")
    after = _call(store).projects[0].last_activity

    assert after > before


def test_prime_on_an_unopenable_store_reports_it_as_data(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "db"
    not_a_directory.write_text("")

    async def go() -> Any:
        return await context.bind(prime, lambda: not_a_directory)(project=None)

    lines = asyncio.run(go()).render_text().splitlines()

    assert lines[0].startswith(
        f"state: store unavailable: cannot create a database at {not_a_directory}"
    )
    assert lines[1:] == ["Next: version()"]
