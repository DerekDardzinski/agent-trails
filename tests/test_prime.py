"""Tests for whole-store and project-scoped prime views."""

from __future__ import annotations

import asyncio
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


def test_project_prime_shows_full_current_framing_and_problem_bodies(store: Store) -> None:
    """Catches scoped prime omitting framing revision guidance or printing ordinary bodies."""
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
    note = _write(store, "Operational note", "Do not print this body.", projects=[project])

    result = _call(store, project="Alpha")
    text = result.render_text()

    assert result.project is not None and result.project.id == project
    assert result.framing is not None and result.framing.version == 2
    assert [record.id for record in result.problem_statements] == [problem]
    assert [entry.record.id for entry in result.groups["note"]] == [note]
    assert "Why Alpha exists." in text
    assert "Full objective body." in text
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
        projects=[project],
    )
    result_id = _write(
        store,
        "Result",
        entry_type="result",
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
