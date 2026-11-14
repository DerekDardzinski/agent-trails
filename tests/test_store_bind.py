"""Tests for placeholder binding in the store (labbook-79y.7)."""

from __future__ import annotations

import pytest

from trails.errors import ValidationError
from trails.models import RecordDraft
from trails.store import Store, _bind


@pytest.fixture
def store(record_store: Store) -> Store:
    """This module tests the old record store, removed with it in the next commit."""
    return record_store


def test_a_param_containing_a_question_mark_is_not_spliced_into_by_the_next_param() -> None:
    """Catches the original defect: substituted text was rescanned for `?`, so the second
    param landed inside the first param's literal and produced malformed or injected SQL."""
    assert _bind("SELECT ?, ?", ["a?", "b"]) == "SELECT 'a?', 'b'"


def test_a_question_mark_inside_quoted_sql_text_is_not_a_placeholder() -> None:
    """Catches a `?` inside a string literal or identifier being counted and replaced."""
    sql = "SELECT 'why?' AS `q?`, \"it\\'s?\", ? AS v"
    assert _bind(sql, ["x"]) == "SELECT 'why?' AS `q?`, \"it\\'s?\", 'x' AS v"


def test_the_placeholder_count_mismatch_is_refused() -> None:
    """Catches a statement silently bound with a param left over or a placeholder unfilled."""
    with pytest.raises(ValueError, match="2 placeholders but 1 params"):
        _bind("SELECT ?, ?", ["a"])


def test_a_query_with_quote_backslash_and_question_mark_params_returns_them_unchanged(
    store: Store,
) -> None:
    """Catches Store.query corrupting agent-supplied text that contains SQL metacharacters."""
    rows = store.query("SELECT ? AS a, ? AS b", ["it's \\ a?", "next?"])
    assert rows == [{"a": "it's \\ a?", "b": "next?"}]


def test_collation_equal_tags_with_question_marks_are_refused_as_validation_errors(
    store: Store,
) -> None:
    """Catches the failed-write classifier raising a raw DoltError, because its two-param
    collation query was corrupted by a tag ending in `?`."""
    with pytest.raises(ValidationError, match="one tag to the store"):
        store.write_record(
            RecordDraft(entry_type="note", work_status="closed", title="t", tags=["cafe?", "café?"])
        )


def test_a_backslash_escaped_quote_does_not_end_the_quoted_section() -> None:
    """labbook-79y.6 item 15. Catches the escape branch dropped, which ends the string at
    the escaped quote and binds the next param into the literal."""
    assert _bind("SELECT 'a\\'?' AS x, ? AS y", ["v"]) == "SELECT 'a\\'?' AS x, 'v' AS y"


def test_a_statement_ending_inside_a_quote_is_refused() -> None:
    """labbook-79y.6 item 15. Catches an unterminated quote read as closed, so placeholders
    after it are bound into what the server parses as string text."""
    with pytest.raises(ValueError, match="statement ends inside a ' quoted section"):
        _bind("SELECT 'abc ?", [])
