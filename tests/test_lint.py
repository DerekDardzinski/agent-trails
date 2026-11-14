"""lint over entries, against real Dolt."""

import asyncio
from typing import Any

from trails.entry_store import EntryStore
from trails.models import EntryDraft
from trails.ops import context
from trails.ops.lint import lint


def run_lint(store: EntryStore, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(lint, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def test_lint_reports_a_link_written_against_an_older_version(store: EntryStore) -> None:
    """Silent failure caught: a claim still counted as support for a statement that has since
    changed under it (dec-o0gww: "supports v2, now v3")."""
    claim = store.write_entry(EntryDraft(kind="note", title="lift is 3%")).entry.id
    note = store.write_entry(EntryDraft(kind="note", title="run log")).entry.id
    store.add_entry_link(note, "supports", claim)
    store.write_entry(EntryDraft(id=claim, base_version=1, reason="rerun", title="lift is 2%"))

    items = run_lint(store).categories["stale-link-versions"]

    assert [(item.entry_id, item.detail) for item in items] == [
        (note, f"supports {claim} written against v1, now v2")
    ]


def test_a_clean_store_has_no_problems(store: EntryStore) -> None:
    store.write_entry(EntryDraft(kind="note", title="fine"))
    result = run_lint(store)
    assert {name: items for name, items in result.categories.items() if items} == {
        "unembedded": result.categories["unembedded"]
    }


def test_lint_flags_a_review_written_by_the_session_that_wrote_the_version(
    store: EntryStore, monkeypatch: Any
) -> None:
    """Silent failure caught: a verdict an author gave its own work passing as an
    independent review, with nothing in the store pointing it out. A review from another
    session is not listed."""
    from trails import sessions

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "author-session")
    token = sessions.CLIENT_NAME.set("claude-code")
    try:
        task = store.write_entry(EntryDraft(kind="task", title="change")).entry.id
        own = store.add_comment(task, "looks fine", verdict="approve").comment.id
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "reviewer-session")
        store.add_comment(task, "checked", verdict="approve")
    finally:
        sessions.CLIENT_NAME.reset(token)

    items = run_lint(store).categories["reviews-by-the-author-session"]

    assert [(item.entry_id, item.detail) for item in items] == [
        (
            task,
            f"review {own} (approve) on v1 was written by session author-session, which also wrote that version",
        )
    ]
