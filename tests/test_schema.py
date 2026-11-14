"""Tests for the schema a fresh database is created with."""

from __future__ import annotations

from pathlib import Path

from trails import dolt
from trails.entry_store import SCHEMA_MARKER, EntryStore, open_entry_store
from trails.models import EntryDraft

TABLES = {
    "comment",
    "comment_addresses",
    "comment_pin",
    "entry",
    "entry_chunk_embedding",
    "entry_project_event",
    "entry_version",
    "link_event",
    "project",
    "project_framing_embedding",
    "project_framing_version",
    "project_path",
    "retirement",
    "session",
    "tag_alias",
    "task_event",
    "version_artifact",
    "version_code",
    "version_tag",
}


def test_a_fresh_database_holds_exactly_the_trails_tables(store: EntryStore) -> None:
    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}

    assert tables == TABLES


def test_every_event_row_records_its_harness(store: EntryStore) -> None:
    """Catches an event table that only a session row can attribute, which loses the harness
    for every write from a harness that gives no session id."""
    for table in (
        "entry_version",
        "link_event",
        "task_event",
        "comment",
        "retirement",
        "entry_project_event",
    ):
        columns = {str(row["Field"]) for row in store.query(f"SHOW COLUMNS FROM {table}")}
        assert {"author", "harness", "session_id"} <= columns, table


def test_every_non_path_text_column_is_case_and_accent_insensitive(store: EntryStore) -> None:
    """Catches a text column left on Dolt's binary default and silent case-sensitive search."""
    rows = store.query(
        "SELECT table_name AS t, column_name AS c, collation_name AS k FROM "
        "information_schema.columns WHERE table_schema = DATABASE() AND data_type IN "
        "('varchar', 'text', 'longtext')"
    )
    binary = sorted(
        (str(row["t"]), str(row["c"])) for row in rows if row["k"] != "utf8mb4_0900_ai_ci"
    )
    assert binary == [("project_path", "path")]
    assert {str(row["t"]) for row in rows} == TABLES


def test_fulltext_and_like_match_regardless_of_case(store: EntryStore) -> None:
    entry = store.write_entry(
        EntryDraft(kind="note", title="Lift Study", body="Variance Reduction")
    ).entry

    by_title = store.query(
        "SELECT entry_id AS id FROM entry_version WHERE MATCH(title) AGAINST (?)", ["lift"]
    )
    by_body = store.query(
        "SELECT entry_id AS id FROM entry_version WHERE MATCH(body) AGAINST (?)", ["variance"]
    )
    by_like = store.query(
        "SELECT entry_id AS id FROM entry_version WHERE body LIKE ?", ["%reduction%"]
    )

    assert by_title == by_body == by_like == [{"id": entry.id}]


def test_a_database_with_no_tables_gets_the_schema_on_open(tmp_path: Path) -> None:
    """Catches a process that died between `dolt init` and the schema leaving a tableless
    database that every later open treats as complete."""
    db = tmp_path / "db"
    dolt.init_database(db)

    store = open_entry_store(db)

    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}
    assert tables == TABLES
    (db / SCHEMA_MARKER).unlink()
    assert open_entry_store(db).query("SELECT COUNT(*) AS n FROM entry") == [{"n": 0}]
