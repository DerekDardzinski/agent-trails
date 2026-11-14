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
    claim = store.write_entry(EntryDraft(kind="claim", title="lift is 3%")).entry.id
    note = store.write_entry(EntryDraft(kind="note", title="run log")).entry.id
    store.add_entry_link(note, "supports", claim)
    store.write_entry(EntryDraft(id=claim, base_version=1, reason="rerun", title="lift is 2%"))

    items = run_lint(store).categories["stale-link-versions"]

    assert [(item.entry_id, item.detail) for item in items] == [
        (note, f"supports {claim} written against v1, now v2")
    ]


def test_lint_reports_a_dependency_cycle(store: EntryStore) -> None:
    """Silent failure caught: tasks that can never become ready, with nothing saying why once
    the write-time warning has scrolled away."""
    first = store.write_entry(EntryDraft(kind="task", title="a")).entry.id
    second = store.write_entry(EntryDraft(kind="task", title="b", waits_on=[first])).entry.id
    store.add_entry_link(first, "waits_on", second)

    items = run_lint(store).categories["dependency-cycles"]

    assert len(items) == 1
    assert items[0].detail.startswith("dependency cycle: ")
    assert set(items[0].detail.split(";")[0].removeprefix("dependency cycle: ").split(" -> ")) == {
        first,
        second,
    }


def test_a_clean_store_has_no_problems(store: EntryStore) -> None:
    store.write_entry(EntryDraft(kind="note", title="fine"))
    result = run_lint(store)
    assert {name: items for name, items in result.categories.items() if items} == {
        "unembedded": result.categories["unembedded"]
    }
