"""Tests for whole-store and project-scoped prime views."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from trails.models import Link, ProjectFramingDraft, RecordDraft
from trails.ops import context
from trails.ops.prime import prime
from trails.ops.search import search
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


def test_project_prime_shows_framing_and_open_problem_titles_but_no_other_record(
    store: Store,
) -> None:
    """Catches the page growing with history again: an open record listed, a body printed, or
    a closed problem statement shown instead of counted."""
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
    _write(store, "Done goal", entry_type="problem-statement", projects=[project])
    _write(store, "Active note", "Note body.", work_status="open", projects=[project])

    text = _call(store, project="Alpha").render_text()

    assert "Why Alpha exists." in text
    assert f'update_project(project="{project}"' in text
    assert 'base_version=2, reason="..."' in text
    assert (
        "open problem statements (1 of 2 current):\n"
        f"  {problem}  Reduce variance\n"
        f'  read them: show(["{problem}"], detail="full")\n'
        "  1 closed: "
        f'search(projects=["{project}"], entry_type="problem-statement", work_status="closed", '
        "limit=1)"
    ) in text
    for hidden in ("Full objective body.", "Done goal", "Active note", "Note body."):
        assert hidden not in text


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


def test_project_prime_counts_each_type_with_calls_that_return_exactly_that_many(
    store: Store,
) -> None:
    """Catches a count that disagrees with its listing call, which is the only way to the
    records prime no longer lists."""
    project = _project(store, "Alpha")
    for title in ("Deciding", "Also deciding"):
        _write(store, title, entry_type="decision", work_status="open", projects=[project])
    _write(store, "Settled", entry_type="decision", projects=[project])
    _write(store, "Old fact", entry_type="fact", projects=[project])
    _write(store, "Goal", entry_type="problem-statement", work_status="open", projects=[project])

    result = _call(store, project=project)
    text = result.render_text()

    assert [(row.entry_type, row.open, row.closed) for row in result.counts] == [
        ("problem-statement", 1, 0),
        ("fact", 0, 1),
        ("decision", 2, 1),
    ]
    assert (
        "  decision: 2 open · 1 closed\n"
        f'    open: search(projects=["{project}"], entry_type="decision", work_status="open", '
        "limit=2)\n"
        f'    closed: search(projects=["{project}"], entry_type="decision", work_status="closed", '
        "limit=1)"
    ) in text
    assert (
        "  fact: 0 open · 1 closed\n"
        f'    closed: search(projects=["{project}"], entry_type="fact", work_status="closed", '
        "limit=1)"
    ) in text

    async def listing() -> Any:
        return await context.bind(search, lambda: store.db_path)(
            projects=[project], entry_type="decision", work_status="open", limit=2
        )

    listed = asyncio.run(listing())
    assert sorted(hit.title for hit in listed.hits) == ["Also deciding", "Deciding"]
    assert listed.total == 2
    assert f'  all 3 open: search(projects=["{project}"], work_status="open", limit=3)' in text

    async def everything_open() -> Any:
        return await context.bind(search, lambda: store.db_path)(
            projects=[project], work_status="open", limit=3
        )

    assert asyncio.run(everything_open()).total == 3
    assert f'search(projects=["{project}"], query="...")' in text


def test_project_prime_lists_contradicted_records_with_current_corrections_only(
    store: Store,
) -> None:
    """Catches a correction hidden by the page, or a retired correction still presented as one."""
    project = _project(store, "Alpha")
    wrong = _write(store, "Wrong total", entry_type="report", projects=[project])
    fix = _write(
        store,
        "Correct total",
        work_status="open",
        projects=[project],
        links=[Link(relation="contradicts", target_id=wrong)],
    )
    withdrawn = _write(
        store,
        "Withdrawn objection",
        projects=[project],
        links=[Link(relation="contradicts", target_id=wrong)],
    )
    store.retire(withdrawn, "Objection was mistaken.")
    untouched = _write(store, "Fine report", entry_type="report", projects=[project])

    text = _call(store, project=project).render_text()

    assert (
        "contradicted records (1; read the correction before relying on these):\n"
        f"  {wrong}  report  Wrong total · work closed\n"
        f"    contradicted by: {fix} (note) Correct total\n"
        f'  read the corrections: show(["{fix}"], detail="full")'
    ) in text
    assert "Withdrawn objection" not in text
    assert untouched not in text
    assert "  report: 0 open · 2 closed" in text


def test_project_prime_gives_the_call_that_reads_a_problem_statements_correction(
    store: Store,
) -> None:
    """Catches a correction of an open problem statement named on the page with no call that
    reads it."""
    project = _project(store, "Alpha")
    goal = _write(
        store, "Goal", entry_type="problem-statement", work_status="open", projects=[project]
    )
    against = _write(
        store,
        "Goal is wrong",
        projects=[project],
        links=[Link(relation="contradicts", target_id=goal)],
    )

    text = _call(store, project=project).render_text()

    assert (
        "contradicted records: none besides problem statements above\n"
        f'  read the corrections: show(["{against}"], detail="full")'
    ) in text


def test_project_prime_structured_content_carries_no_record_body(store: Store) -> None:
    """Catches the 10.6 MB reply: full records in the structured content an MCP host must
    accept in one message."""
    project = _project(store, "Alpha")
    problem = _write(
        store,
        "Goal",
        "PROBLEM-BODY",
        entry_type="problem-statement",
        work_status="open",
        projects=[project],
    )
    _write(store, "Measured", "RESULT-BODY", entry_type="result", projects=[project])

    dumped = _call(store, project=project).model_dump(mode="json")

    serialized = json.dumps(dumped)
    assert "PROBLEM-BODY" not in serialized
    assert "RESULT-BODY" not in serialized
    assert dumped["problem_statements"] == [
        {"id": problem, "entry_type": "problem-statement", "title": "Goal", "work_status": "open"}
    ]


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
