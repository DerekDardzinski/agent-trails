"""Tests for the schema a fresh database is created with."""

from __future__ import annotations

from pathlib import Path

from trails import dolt
from trails.models import RecordDraft
from trails.store import SCHEMA_MARKER, Store, open_store


def test_a_fresh_database_holds_exactly_the_trails_tables(store: Store) -> None:
    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}

    assert tables == {
        "artifact",
        "code_capture",
        "link",
        "project",
        "project_framing_embedding",
        "project_framing_version",
        "project_path",
        "record",
        "record_chunk_embedding",
        "record_project_event",
        "record_tag",
        "record_work_status",
        "retirement",
        "tag_alias",
        "verified_stamp",
    }


def test_project_schema_has_stable_identity_framing_and_membership_events(store: Store) -> None:
    """Catches an intermediate dual-read schema or wrong event keys surviving the cutover."""
    record_columns = {str(row["Field"]) for row in store.query("SHOW COLUMNS FROM record")}
    project_columns = {str(row["Field"]) for row in store.query("SHOW COLUMNS FROM project")}
    framing_columns = {
        str(row["Field"]) for row in store.query("SHOW COLUMNS FROM project_framing_version")
    }
    event_columns = {
        str(row["Field"]): (str(row["Type"]), str(row["Key"]))
        for row in store.query("SHOW COLUMNS FROM record_project_event")
    }

    assert "project" not in record_columns
    assert project_columns == {"id", "name", "created_at", "created_by", "created_on_host"}
    assert framing_columns == {
        "project_id",
        "version",
        "title",
        "body",
        "reason",
        "author",
        "created_at",
    }
    assert set(event_columns) == {
        "record_id",
        "project_id",
        "sequence",
        "action",
        "reason",
        "author",
        "created_at",
    }
    assert event_columns["action"][0] == "enum('add','remove')"
    assert [
        str(row["Column_name"])
        for row in store.query("SHOW INDEX FROM record_project_event WHERE Key_name = 'PRIMARY'")
    ] == ["record_id", "project_id", "sequence"]
    path_collation = store.query(
        "SELECT COLLATION_NAME FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = 'project_path' AND column_name = 'path'"
    )[0]["COLLATION_NAME"]
    assert path_collation == "utf8mb4_0900_bin"


def test_every_non_path_text_column_is_case_and_accent_insensitive(store: Store) -> None:
    """Catches a text column left on Dolt's binary default and silent case-sensitive search."""
    for table in (
        "record",
        "record_tag",
        "record_work_status",
        "artifact",
        "project",
        "project_framing_version",
        "record_project_event",
    ):
        text_columns = {
            str(row["Field"]): str(row["Type"])
            for row in store.query(f"SHOW FULL COLUMNS FROM {table}")
            if str(row["Type"]).lower().startswith(("varchar", "text", "longtext"))
        }
        assert text_columns, table
        assert all(
            declared.endswith("COLLATE utf8mb4_0900_ai_ci") for declared in text_columns.values()
        )

    path_columns = {
        str(row["Field"]): str(row["Type"])
        for row in store.query("SHOW FULL COLUMNS FROM project_path")
        if str(row["Type"]).lower().startswith(("varchar", "text", "longtext"))
    }
    assert (
        store.query(
            "SELECT COLLATION_NAME FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = 'project_path' AND column_name = 'path'"
        )[0]["COLLATION_NAME"]
        == "utf8mb4_0900_bin"
    )
    assert all(
        declared.endswith("COLLATE utf8mb4_0900_ai_ci")
        for column, declared in path_columns.items()
        if column != "path"
    )


def test_fulltext_and_like_match_regardless_of_case(store: Store) -> None:
    record = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="Lift Study",
            body="Variance Reduction",
        )
    ).record

    by_title = store.query("SELECT id FROM record WHERE MATCH(title) AGAINST (?)", ["lift"])
    by_body = store.query("SELECT id FROM record WHERE MATCH(body) AGAINST (?)", ["variance"])
    by_like = store.query("SELECT id FROM record WHERE body LIKE ?", ["%reduction%"])

    assert by_title == by_body == by_like == [{"id": record.id}]


def test_a_database_with_no_tables_gets_the_schema_on_open(tmp_path: Path) -> None:
    db = tmp_path / "db"
    dolt.init_database(db)

    store = open_store(db)

    tables = {str(next(iter(row.values()))) for row in store.query("SHOW TABLES")}
    assert {"record", "link", "project", "record_project_event"} <= tables
    (db / SCHEMA_MARKER).unlink()
    assert open_store(db).query("SELECT COUNT(*) AS n FROM record") == [{"n": 0}]
