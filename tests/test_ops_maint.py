"""Tests for the export, alias, mark_verified and embed operations."""

import asyncio
import time
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine, basis
from trails import engine as engine_seam
from trails.errors import ValidationError
from trails.models import RecordDraft
from trails.ops import context, mark_verified as mark_verified_module
from trails.ops.alias import alias
from trails.ops.embed import embed
from trails.ops.export import export
from trails.ops.mark_verified import mark_verified
from trails.ops.publish import publish
from trails.store import Store


def _run(store: Store, operation: Any, **arguments: Any) -> Any:
    if operation is publish and "from_file" not in arguments:
        arguments.setdefault("entry_type", "note")
        arguments.setdefault("work_status", "closed")

    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def test_an_exported_file_with_a_reason_line_publishes_a_superseding_record(
    store: Store, tmp_path: Path
) -> None:
    """Silent failure: an exported file that publish cannot read back, or that publishes as an
    unrelated new record instead of superseding the one it came from."""
    old = store.write_record(
        RecordDraft(
            work_status="closed",
            title="Sync green to red",
            body="1. step one\n",
            entry_type="procedure",
        )
    ).record
    path = tmp_path / "nested" / "dir" / "sync.md"

    exported = _run(store, export, record_id=old.id, path=str(path))
    text = path.read_text()
    edited = text.replace("1. step one", "1. step one, fixed").replace(
        "+++\n", '+++\nreason = "step one was wrong"\n', 1
    )
    path.write_text(edited)
    published = _run(store, publish, from_file=str(path))

    assert exported.path == str(path)
    assert exported.render_text().splitlines()[-1] == f'Next: publish(from_file="{path}")'
    assert published.superseded_id == old.id
    assert store.get_record(old.id).superseded_by == published.record_id
    new = store.get_record(published.record_id)
    assert (new.body, new.reason, new.entry_type, new.tags) == (
        "1. step one, fixed\n",
        "step one was wrong",
        "procedure",
        [],
    )


def test_export_refuses_an_existing_file_unless_overwrite(store: Store, tmp_path: Path) -> None:
    """Silent failure: an export clobbering a file the agent was still editing."""
    record = store.write_record(
        RecordDraft(work_status="closed", title="project", entry_type="project-context")
    ).record
    path = tmp_path / "project-context.md"
    path.write_text("my unsaved edits")

    with pytest.raises(ValidationError, match="exists; pass overwrite=true"):
        _run(store, export, record_id=record.id, path=str(path))
    assert path.read_text() == "my unsaved edits"

    _run(store, export, record_id=record.id, path=str(path), overwrite=True)
    assert path.read_text().startswith(f'+++\nid = "{record.id}"\n')


def test_alias_stores_the_normalised_pair(store: Store) -> None:
    """Silent failure: an alias stored under a spelling no record tag can carry."""
    result = _run(store, alias, tag=" Lift ", canonical="Uplift")

    assert store.alias_map() == {"lift": "uplift"}
    assert result.render_text() == (
        'alias lift -> uplift: a search for either finds both\nNext: search(tags=["uplift"])'
    )


def test_mark_verified_stamps_today_and_the_environment(store: Store) -> None:
    """Silent failure: a stamp with the wrong date or environment, so the procedure reads as
    stale, or verified somewhere it was not."""
    record = store.write_record(
        RecordDraft(work_status="closed", title="Sync", entry_type="procedure")
    ).record
    today = datetime.now().astimezone().date()

    result = _run(store, mark_verified, record_id=record.id, environment="green")

    assert store.get_record(record.id).last_verified == (today, "green")
    assert result.render_text().splitlines()[-1] == f'Next: show("{record.id}")'


def test_embed_writes_vectors_only_for_records_without_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: a backfill that re-embeds the whole store, or skips the records that
    publish left unembedded."""
    fake = FakeEngine({"Done": basis(0), "Pending": basis(1)})
    done = store.write_record(
        RecordDraft(work_status="closed", title="Done", entry_type="note")
    ).record
    store.upsert_chunk_embeddings([(done.id, 0, fake.model_id(), basis(0))])
    store.write_record(RecordDraft(work_status="closed", title="Pending", entry_type="note"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: fake)

    result = _run(store, embed)

    assert (result.records, result.vectors) == (1, 1)
    assert fake.calls == ["Pending"]
    assert result.render_text() == ("1 embedding vectors written for 1 records\nNext: search(...)")


def test_embed_with_no_runtime_writes_nothing_and_says_how_to_install_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 10. Silent failure: a backfill that reports success, or raises,
    when the optional embedding runtime is missing."""
    store.write_record(RecordDraft(work_status="closed", title="Unembedded", entry_type="note"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: FakeEngine(available=False))

    result = _run(store, embed)

    assert (result.available, result.records, result.vectors) == (False, 0, 0)
    assert store.query("SELECT COUNT(*) AS n FROM record_chunk_embedding") == [{"n": 0}]
    assert result.render_text() == (
        "embedding runtime not available: install the search extra "
        "(uv sync --extra search); nothing was written"
    )


def test_mark_verified_stamps_the_local_date_not_the_utc_one(
    store: Store, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """labbook-79y.6 item 10. Silent failure: a stamp taken in UTC, so a procedure verified
    on a California evening is stamped tomorrow."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    request.addfinalizer(time.tzset)
    instant = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 2026-10-01 20:00 in Los Angeles

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Any:
            return (
                instant.astimezone(tz)
                if tz is not None
                else instant.astimezone().replace(tzinfo=None)
            )

    monkeypatch.setattr(mark_verified_module, "datetime", Frozen)
    record = store.write_record(
        RecordDraft(work_status="closed", title="Sync", entry_type="procedure")
    ).record

    _run(store, mark_verified, record_id=record.id, environment="red")

    assert store.get_record(record.id).last_verified == (date(2026, 10, 1), "red")


def test_a_revised_verified_procedure_starts_unverified_and_says_so(store: Store) -> None:
    """Catches a verification stamp silently carrying to changed steps, or the publish output
    leaving the agent to assume that it did."""
    old = store.write_record(
        RecordDraft(work_status="closed", title="Run it", entry_type="procedure")
    ).record
    store.stamp_verified(old.id, "local-mac", date(2026, 10, 6))

    revised = _run(
        store,
        publish,
        title="Run it with the corrected flag",
        entry_type="procedure",
        supersedes=old.id,
        reason="the old flag was wrong",
    )
    new = store.get_record(revised.record_id)

    assert new.last_verified is None
    assert revised.notes == [
        "superseding records inherit no links, artifacts, captured code or verification "
        "stamps; this record contains only what this call supplied"
    ]
    assert revised.warnings == [
        f"the procedure this replaces was verified 2026-10-06 in local-mac; "
        f"{new.id} starts unverified",
    ]
