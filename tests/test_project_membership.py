"""Tests for append-only zero-to-many project membership."""

from __future__ import annotations

import asyncio
import fcntl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from trails import dolt
from trails.errors import ValidationError
from trails.models import RecordDraft
from trails.ops import context
from trails.ops.publish import publish
from trails.ops.set_projects import set_projects
from trails.ops.show import show
from trails.store import Store


def _call(store: Store, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _project(store: Store, name: str) -> str:
    return store.write_project(name, "host", [], []).project.id


def _publish(store: Store, title: str, **arguments: Any) -> Any:
    return _call(
        store,
        publish,
        entry_type="note",
        work_status="closed",
        title=title,
        **arguments,
    )


def _commits(store: Store) -> int:
    return int(store.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def test_publish_lands_two_sequence_zero_memberships_and_echoes_record_fields(store: Store) -> None:
    """Catches memberships written after the record or PublishResult omitting what landed."""
    alpha, beta = _project(store, "Alpha"), _project(store, "Beta")

    result = _publish(store, "Shared", projects=["Alpha", beta, alpha])

    assert (result.entry_type, result.work_status) == ("note", "closed")
    assert [(item.id, item.name) for item in result.projects] == [
        (alpha, "Alpha"),
        (beta, "Beta"),
    ]
    assert {
        (row["project_id"], row["sequence"], row["action"], row.get("reason"))
        for row in store.query(
            "SELECT project_id, sequence, action, reason FROM record_project_event "
            "WHERE record_id = ? ORDER BY project_id",
            [result.record_id],
        )
    } == {(alpha, 0, "add", None), (beta, 0, "add", None)}


def test_set_projects_changes_grouping_without_changing_the_record(store: Store) -> None:
    """Catches regrouping revising record content or failing to batch mixed add/remove."""
    alpha, beta = _project(store, "Alpha"), _project(store, "Beta")
    record = store.write_record(
        RecordDraft(entry_type="fact", work_status="open", title="Fact", body="Body")
    ).record

    first = _call(
        store,
        set_projects,
        record_id=record.id,
        add=["Alpha", beta],
        reason="Grouped after discovery.",
    )
    mixed = _call(
        store,
        set_projects,
        record_id=record.id,
        add=[],
        remove=[alpha],
        reason="Only Beta uses it.",
    )
    after = store.get_record(record.id)

    assert [change.action for change in first.changed] == ["add", "add"]
    assert [change.action for change in mixed.changed] == ["remove"]
    assert [(item.id, item.name) for item in after.projects] == [(beta, "Beta")]
    assert (after.title, after.body, after.work_status, after.links) == (
        record.title,
        record.body,
        record.work_status,
        record.links,
    )
    events = store.project_membership_history([record.id])[record.id]
    actual = [(event.project.id, event.sequence, event.action) for event in events]
    assert actual[0] == (alpha, 1, "remove")
    assert actual[1:] == sorted([(alpha, 0, "add"), (beta, 0, "add")])


def test_set_projects_idempotent_retry_writes_no_event_or_commit(store: Store) -> None:
    """Catches a no-op retry reaching CALL DOLT_COMMIT or appending false changes."""
    alpha, beta = _project(store, "Alpha"), _project(store, "Beta")
    record = _publish(store, "Scoped", projects=[alpha])
    before = _commits(store)

    result = _call(
        store,
        set_projects,
        record_id=record.record_id,
        add=["Alpha"],
        remove=[beta],
        reason="Retry.",
    )

    assert result.changed == []
    assert [item.id for item in result.unchanged_add] == [alpha]
    assert [item.id for item in result.unchanged_remove] == [beta]
    assert _commits(store) == before


def test_set_projects_refuses_unknown_overlap_empty_and_no_requested_values(store: Store) -> None:
    alpha = _project(store, "Alpha")
    record = _publish(store, "Scoped", projects=[alpha])

    with pytest.raises(ValidationError, match="no registered project"):
        _call(
            store,
            set_projects,
            record_id=record.record_id,
            add=["Missing"],
            reason="No.",
        )
    with pytest.raises(ValidationError, match="both add and remove"):
        _call(
            store,
            set_projects,
            record_id=record.record_id,
            add=[alpha],
            remove=["Alpha"],
            reason="No.",
        )
    with pytest.raises(ValidationError, match="needs at least one"):
        _call(store, set_projects, record_id=record.record_id, reason="No values.")
    with pytest.raises(ValidationError, match="needs a reason"):
        _call(store, set_projects, record_id=record.record_id, remove=[alpha])


def test_supersede_omitted_inherits_explicit_replaces_and_empty_unscopes(store: Store) -> None:
    """Catches normal revision silently disappearing from projects or omitted and empty lists
    being collapsed into the same behavior."""
    alpha, beta = _project(store, "Alpha"), _project(store, "Beta")
    first = _publish(store, "v1", projects=[alpha, beta])
    inherited = _publish(
        store,
        "v2",
        supersedes=first.record_id,
        reason="Revised.",
        projects=None,
    )
    explicit = _publish(
        store,
        "v3",
        supersedes=inherited.record_id,
        reason="Narrowed.",
        projects=[beta],
    )
    unscoped = _publish(
        store,
        "v4",
        supersedes=explicit.record_id,
        reason="Detached.",
        projects=[],
    )

    assert [item.id for item in inherited.projects] == [alpha, beta]
    assert [item.id for item in explicit.projects] == [beta]
    assert unscoped.projects == []


def test_full_show_includes_ordered_membership_history(store: Store) -> None:
    alpha = _project(store, "Alpha")
    record = _publish(store, "History")
    _call(
        store,
        set_projects,
        record_id=record.record_id,
        add=[alpha],
        reason="Added.",
    )
    _call(
        store,
        set_projects,
        record_id=record.record_id,
        remove=[alpha],
        reason="Removed.",
    )

    result = _call(store, show, item_id=record.record_id, detail="full")

    assert [event.action for event in result.records[0].membership_history] == ["remove", "add"]
    assert "project membership history:" in result.render_text()


def test_supersession_reads_inherited_memberships_under_the_exclusive_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches set_projects landing between inheritance read and the superseding record write."""
    alpha = _project(store, "Alpha")
    first = _publish(store, "v1", projects=[alpha])
    real_read = dolt.read_result_sets
    held: list[bool] = []
    lock_path = store.db_path / dolt.WRITE_LOCK_NAME

    def checking(db_path: Path, statements: list[str]) -> Any:
        if any("FROM record_project_event" in statement for statement in statements):
            with lock_path.open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    held.append(False)
                    fcntl.flock(other, fcntl.LOCK_UN)
        return real_read(db_path, statements)

    monkeypatch.setattr(dolt, "read_result_sets", checking)
    _publish(store, "v2", supersedes=first.record_id, reason="Revised.")

    assert held and held[-1] is True


def test_concurrent_membership_adds_to_different_projects_both_persist(store: Store) -> None:
    """Catches one set_projects commit making another Dolt process read-only or losing its row."""
    alpha, beta = _project(store, "Alpha"), _project(store, "Beta")
    record = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="Concurrent")
    ).record

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda project_id: store.set_projects(
                    record.id, [project_id], [], "Concurrent grouping."
                ),
                [alpha, beta],
            )
        )

    assert all(len(outcome.changed) == 1 for outcome in outcomes)
    assert {project.id for project in store.get_record(record.id).projects} == {alpha, beta}
