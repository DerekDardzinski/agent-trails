"""Tests for the schema a fresh database is created with."""

from __future__ import annotations

from pathlib import Path

from trails import dolt
from trails.models import RecordDraft
from trails.store import SCHEMA_MARKER, Store, open_store


def test_a_fresh_database_holds_exactly_the_trails_tables(store: Store) -> None:
    """Catches a leftover per-kind table, or a missing one, from the schema rewrite.

    A stray `claim` or `verification` table would sit empty and unread, and its presence would
    tell a later reader the strict-workflow store still exists.
    """
    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}

    assert tables == {
        "artifact",
        "code_capture",
        "link",
        "project",
        "project_path",
        "record",
        "record_chunk_embedding",
        "record_tag",
        "record_work_status",
        "retirement",
        "tag_alias",
        "verified_stamp",
    }


def test_every_text_column_of_record_record_tag_and_artifact_is_case_insensitive(
    store: Store,
) -> None:
    """Catches a text column left on Dolt's default utf8mb4_0900_bin.

    That collation makes MATCH and LIKE case-sensitive, so a search for `lift` silently misses
    a title reading `Lift`. Dolt 2.4.0 reports a declared column collation inside the `Type`
    field of SHOW FULL COLUMNS (its `Collation` field reads utf8mb4_0900_bin regardless), and
    in information_schema COLLATION_NAME, so both are asserted.
    """
    for table in ("record", "record_tag", "record_work_status", "artifact"):
        text_columns = {
            str(row["Field"]): str(row["Type"])
            for row in store.query(f"SHOW FULL COLUMNS FROM {table}")
            if str(row["Type"]).lower().startswith(("varchar", "text", "longtext"))
        }
        assert text_columns, table
        for column, declared in text_columns.items():
            assert declared.endswith("COLLATE utf8mb4_0900_ai_ci"), (table, column, declared)

        collations = {
            str(row["COLUMN_NAME"]): str(row["COLLATION_NAME"])
            for row in store.query(
                "SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = ? "
                "AND COLLATION_NAME IS NOT NULL",
                [table],
            )
        }
        assert {column: collations[column] for column in text_columns} == dict.fromkeys(
            text_columns, "utf8mb4_0900_ai_ci"
        ), table


def test_fulltext_and_like_match_regardless_of_case(store: Store) -> None:
    """The behaviour the collation exists for, on both FULLTEXT indexes and LIKE."""
    record = store.write_record(
        RecordDraft(
            entry_type="note", work_status="closed", title="Lift Study", body="Variance Reduction"
        )
    ).record

    by_title = store.query("SELECT id FROM record WHERE MATCH(title) AGAINST (?)", ["lift"])
    by_body = store.query("SELECT id FROM record WHERE MATCH(body) AGAINST (?)", ["variance"])
    by_like = store.query("SELECT id FROM record WHERE body LIKE ?", ["%reduction%"])

    assert by_title == by_body == by_like == [{"id": record.id}]


def test_a_database_with_no_tables_gets_the_schema_on_open(tmp_path: Path) -> None:
    """labbook-79y.6 item 1. Catches a process dying between `dolt init` and the schema
    script leaving a store that opens with no tables, forever."""
    db = tmp_path / "db"
    dolt.init_database(db)

    store = open_store(db)

    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}
    assert {"record", "link", "project"} <= tables
    (db / SCHEMA_MARKER).unlink()
    assert open_store(db).query("SELECT COUNT(*) AS n FROM record") == [{"n": 0}]
