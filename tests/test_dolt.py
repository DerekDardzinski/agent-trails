"""Tests for the Dolt subprocess boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trails.dolt import init_database, read_rows, run_script, sql_literal
from trails.errors import DoltError, StoreUnavailableError


def test_sql_literal_escapes_quote_and_backslash() -> None:
    """An unescaped quote terminates the literal early and shifts which column
    receives which value, so a claim is stored with its fields transposed rather
    than with a visible error."""
    assert sql_literal("it's a\\b") == r"'it\'s a\\b'"


def test_sql_literal_datetime_is_utc_naive_string() -> None:
    """Dolt DATETIME rejects an offset suffix and silently accepts a local-time
    string, so a wrong conversion moves created_at by hours and every ordering by
    time is wrong."""
    aware = datetime(2026, 8, 5, 21, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert sql_literal(aware) == "'2026-08-06 04:00:00.000000'"


def test_read_rows_raises_store_unavailable_outside_a_dolt_database(tmp_path) -> None:
    """`dolt sql` exits 0 in a non-Dolt directory, so without the guard a read
    against an uninitialised path returns [] and an absent store is
    indistinguishable from an empty one."""
    with pytest.raises(StoreUnavailableError):
        read_rows(tmp_path, "SELECT 1 AS n")

    init_database(tmp_path)
    assert read_rows(tmp_path, "SELECT 1 AS n") == [{"n": 1}]


def test_init_database_translates_an_unusable_path(tmp_path) -> None:
    """A raw OSError here escapes the CLI's error handling as a traceback, so a
    mistyped --db is indistinguishable from a crash — and `open_store` contracts to
    raise StoreUnavailableError when the database cannot be opened."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    with pytest.raises(StoreUnavailableError, match="cannot create a database"):
        init_database(blocker / "db")


def test_run_script_without_commit_leaves_no_row(tmp_path) -> None:
    """If Dolt autocommitted each statement of a multi-statement `-q`, an
    interrupted write would leave a claim row with no verification row — a claim
    that reads as never-checked. The whole write path rests on this."""
    init_database(tmp_path)
    run_script(tmp_path, ["CREATE TABLE t (id VARCHAR(8) PRIMARY KEY)"])

    inserts = ["START TRANSACTION", "INSERT INTO t VALUES ('a')", "INSERT INTO t VALUES ('b')"]
    run_script(tmp_path, inserts)
    assert read_rows(tmp_path, "SELECT COUNT(*) AS n FROM t") == [{"n": 0}]

    run_script(tmp_path, [*inserts, "COMMIT"])
    assert read_rows(tmp_path, "SELECT COUNT(*) AS n FROM t") == [{"n": 2}]


def test_run_script_raises_dolt_error_with_stderr(tmp_path) -> None:
    """A swallowed non-zero exit makes a failed write indistinguishable from a
    successful one, so write_claim would return a Claim that is not in the
    database."""
    init_database(tmp_path)
    with pytest.raises(DoltError, match="nosuchtable"):
        run_script(tmp_path, ["INSERT INTO nosuchtable VALUES (1)"])
