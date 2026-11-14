"""The entry store's versions, provenance and schema guards, against real Dolt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trails import dolt, sessions
from trails.entry_store import EntryStore, open_entry_store
from trails.errors import StoreUnavailableError, ValidationError
from trails.models import ArtifactRef, EntryDraft
from trails.sessions import ProcessRow

KIRO_NODE = "/Applications/kiro-cli/node /Applications/kiro-cli/kas/index.js"


def in_kiro_session(monkeypatch: pytest.MonkeyPatch, home: Path, session: str, agent: str) -> None:
    """Make this process look like a descendant of a Kiro process holding `session`."""
    leases = home / ".kiro" / "session-leases"
    leases.mkdir(parents=True, exist_ok=True)
    for old in leases.glob("*.lease"):
        old.unlink()
    (leases / f"{session}.lease").write_text(json.dumps({"pid": 4242}))
    folder = home / ".kiro" / "sessions" / "abc" / session
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "messages.jsonl").write_text("{}\n")
    (folder / "session.json").write_text(json.dumps({"agentMode": agent}))
    monkeypatch.setenv("HOME", str(home))
    import os

    me = os.getpid()
    monkeypatch.setattr(
        sessions,
        "process_table",
        lambda: {me: ProcessRow(me, 4242, "python"), 4242: ProcessRow(4242, 1, KIRO_NODE)},
    )


def test_a_new_version_keeps_the_id_and_the_history(entries: EntryStore) -> None:
    """Silent failure caught: an edit replacing the content in place, losing what the entry
    said before, or minting a new id that breaks every link to it."""
    first = entries.write_entry(
        EntryDraft(kind="claim", title="Lift is 3%", body="from run 1", tags=["ab-test"])
    )
    entry_id = first.entry.id
    assert entry_id.startswith("clm-")
    second = entries.write_entry(
        EntryDraft(
            id=entry_id,
            base_version=1,
            reason="rerun with the fixed filter",
            title="Lift is 2%",
            tags=["ab-test", "rerun"],
        )
    )
    assert second.entry.id == entry_id
    assert second.entry.current.version == 2
    versions = entries.get_versions(entry_id)
    assert [(v.version, v.title, v.tags, v.reason) for v in versions] == [
        (1, "Lift is 3%", ["ab-test"], None),
        (2, "Lift is 2%", ["ab-test", "rerun"], "rerun with the fixed filter"),
    ]
    read = entries.get_entry(entry_id)
    assert (read.current.version, read.current.title, read.current.body) == (
        2,
        "Lift is 2%",
        "from run 1",
    )


def test_stale_and_missing_base_version_are_refused_with_the_retry_call(
    entries: EntryStore,
) -> None:
    """Silent failure caught: an edit made from an old read overwriting a newer version."""
    entry_id = entries.write_entry(EntryDraft(kind="note", title="first")).entry.id
    entries.write_entry(EntryDraft(id=entry_id, base_version=1, reason="r", body="two"))
    with pytest.raises(ValidationError) as stale:
        entries.write_entry(EntryDraft(id=entry_id, base_version=1, reason="r", body="three"))
    message = str(stale.value)
    assert "base_version=1 is stale" in message
    assert f"{entry_id} is at version 2" in message
    assert f'write(id="{entry_id}", base_version=2' in message
    with pytest.raises(ValidationError) as missing:
        entries.write_entry(EntryDraft(id=entry_id, reason="r", body="three"))
    assert "needs base_version" in str(missing.value)
    assert f'write(id="{entry_id}", base_version=2' in str(missing.value)
    assert [v.body for v in entries.get_versions(entry_id)] == ["", "two"]


def test_omitted_fields_inherit_and_an_empty_list_clears(entries: EntryStore) -> None:
    """Silent failure caught: a version that changes only the body silently dropping the
    entry's tags and artifacts, or an explicit [] being ignored."""
    created = entries.write_entry(
        EntryDraft(
            kind="task",
            title="t",
            body="b1",
            tags=["infra"],
            artifacts=[ArtifactRef(location="s3://bucket/key", notes="v1")],
            priority=1,
            value={"n": 1},
        )
    ).entry
    kept = entries.write_entry(
        EntryDraft(id=created.id, base_version=1, reason="body only", body="b2")
    ).entry.current
    assert (kept.tags, [a.location for a in kept.artifacts], kept.priority, kept.value) == (
        ["infra"],
        ["s3://bucket/key"],
        1,
        {"n": 1},
    )
    cleared = entries.write_entry(
        EntryDraft(id=created.id, base_version=2, reason="clear", tags=[], artifacts=[], value={})
    ).entry.current
    assert (cleared.tags, cleared.artifacts, cleared.value, cleared.body) == ([], [], None, "b2")
    assert entries.get_entry(created.id).current.tags == []


def test_an_identical_version_is_not_written(entries: EntryStore) -> None:
    """Silent failure caught: a no-op edit adding a version, which makes every other writer's
    base stale for nothing."""
    entry_id = entries.write_entry(EntryDraft(kind="note", title="n", body="x")).entry.id
    outcome = entries.write_entry(EntryDraft(id=entry_id, base_version=1, reason="same", body="x"))
    assert (outcome.created, outcome.retry) == (False, False)
    assert outcome.warnings[0].startswith("nothing changed")
    assert [v.version for v in entries.get_versions(entry_id)] == [1]


def test_a_lost_response_retry_with_the_old_base_is_recognised(entries: EntryStore) -> None:
    """Silent failure caught: a retry after a lost response refused as stale, so the agent
    thinks its edit failed although it landed."""
    entry_id = entries.write_entry(EntryDraft(kind="note", title="n")).entry.id
    draft = EntryDraft(id=entry_id, base_version=1, reason="edit", body="new")
    entries.write_entry(draft)
    again = entries.write_entry(draft)
    assert again.retry is True
    assert [v.version for v in entries.get_versions(entry_id)] == [1, 2]


def test_kind_specific_fields_are_refused_on_other_kinds(entries: EntryStore) -> None:
    """Silent failure caught: a priority stored on a note, where nothing ever reads it."""
    with pytest.raises(ValidationError, match="priority is a goal and task field"):
        entries.write_entry(EntryDraft(kind="note", title="n", priority=1))
    with pytest.raises(ValidationError, match="stated_by is recorded on claims"):
        entries.write_entry(EntryDraft(kind="task", title="t", stated_by="person"))
    claim = entries.write_entry(EntryDraft(kind="claim", title="c")).entry
    assert claim.current.stated_by == "agent"


def test_harness_is_recorded_when_there_is_no_session(entries: EntryStore) -> None:
    """Silent failure caught: a write from a harness that gives no session id recorded with no
    trace of where it came from."""
    token = sessions.CLIENT_NAME.set("some-editor")
    try:
        written = entries.write_entry(EntryDraft(kind="note", title="n")).entry
    finally:
        sessions.CLIENT_NAME.reset(token)
    stored = entries.get_entry(written.id).current
    assert (stored.harness, stored.session_id) == ("some-editor", None)


def test_a_session_row_is_written_once_per_session(
    entries: EntryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: a later write rewriting the session's first-seen time or agent,
    or a second write failing on the session's primary key."""
    in_kiro_session(monkeypatch, tmp_path / "home", "sess_aaa", agent="lab-lead")
    first = entries.write_entry(EntryDraft(kind="note", title="one")).entry
    in_kiro_session(monkeypatch, tmp_path / "home", "sess_aaa", agent="renamed")
    second = entries.write_entry(EntryDraft(kind="note", title="two")).entry
    assert (first.current.session_id, second.current.session_id) == ("sess_aaa", "sess_aaa")
    row = entries.get_session("sess_aaa")
    assert row is not None
    assert (row.harness, row.agent, row.first_seen) == ("kiro", "lab-lead", first.created_at)
    assert row.transcript == str(
        tmp_path / "home" / ".kiro" / "sessions" / "abc" / "sess_aaa" / "messages.jsonl"
    )
    assert len(entries.query("SELECT id FROM session")) == 1


def test_an_old_schema_database_is_refused(tmp_path: Path) -> None:
    """Silent failure caught: the new store creating its tables beside an old record database,
    which would then read as an empty store while the old records sit unseen."""
    old = tmp_path / "old"
    dolt.init_database(old)
    dolt.run_script(
        old,
        [
            "CREATE TABLE record (id VARCHAR(32) PRIMARY KEY, title TEXT)",
            "CALL DOLT_COMMIT('-A','-m','old schema')",
        ],
    )
    with pytest.raises(StoreUnavailableError, match="old trails record schema"):
        open_entry_store(old)
    assert not (old / ".agent-trails-schema").exists()


def test_parent_and_waits_on_are_only_set_at_creation(entries: EntryStore) -> None:
    """Silent failure caught: an edge passed with a version being dropped silently."""
    parent = entries.write_entry(EntryDraft(kind="goal", title="g")).entry.id
    task = entries.write_entry(EntryDraft(kind="task", title="t", parent=parent)).entry
    assert [(link.relation, link.target_id, link.target_version) for link in task.links] == [
        ("part_of", parent, 1)
    ]
    with pytest.raises(ValidationError, match="set when an entry is created"):
        entries.write_entry(EntryDraft(id=task.id, base_version=1, reason="r", waits_on=[parent]))
