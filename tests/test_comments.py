"""Comments, responses, verdicts and pins, against real Dolt."""

from __future__ import annotations

import pytest

from trails.entry_store import EntryStore, unchanged_responses
from trails.errors import ValidationError
from trails.models import EntryDraft


def make(entries: EntryStore, kind: str, title: str, **fields: object) -> str:
    return entries.write_entry(EntryDraft(kind=kind, title=title, **fields)).entry.id  # ty: ignore[invalid-argument-type]


def test_addresses_must_name_an_open_comment_on_the_same_entry(entries: EntryStore) -> None:
    """Silent failure caught: a response closing feedback on a different entry, or closing
    feedback that was already answered, so the open count stops meaning anything."""
    first = make(entries, "task", "a")
    second = make(entries, "task", "b")
    on_first = entries.add_comment(first, "needs a test").comment.id
    on_second = entries.add_comment(second, "rename this").comment.id
    with pytest.raises(ValidationError, match=f"{on_second} is not a comment on {first}"):
        entries.add_comment(first, "done", addresses=[on_second])
    response = entries.add_comment(first, "added", addresses=[on_first]).comment.id
    with pytest.raises(ValidationError, match=f"already addressed by {response}"):
        entries.add_comment(first, "again", addresses=[on_first])
    with pytest.raises(ValidationError, match="is a response and is never open"):
        entries.add_comment(first, "re", addresses=[response])
    with pytest.raises(ValidationError, match="cmt-zzzzz is not a comment on"):
        entries.add_comment(first, "x", addresses=["cmt-zzzzz"])


def test_a_response_closes_what_it_addresses_and_is_never_open(entries: EntryStore) -> None:
    """Silent failure caught: a response counted as new open feedback, so every answer looks
    like another question."""
    task = make(entries, "task", "t")
    feedback = entries.add_comment(task, "why this order?").comment.id
    assert entries.open_comment_counts([task]) == {task: 1}
    entries.add_comment(task, "because of the lock", addresses=[feedback])
    page = entries.get_comments(task)
    assert (page.total, page.open) == (2, 0)
    assert entries.open_comment_counts([task]) == {}
    response, original = page.comments
    assert (response.addresses, original.addressed_by) == ([feedback], [response.id])


def test_a_same_version_response_is_detectable(entries: EntryStore) -> None:
    """Silent failure caught: feedback marked addressed although the entry never changed,
    with nothing to tell a reviewer so."""
    task = make(entries, "task", "t")
    feedback = entries.add_comment(task, "fix the title", verdict="changes_requested").comment
    answered = entries.add_comment(task, "won't fix", addresses=[feedback.id]).comment
    second = entries.add_comment(task, "and the body").comment
    entries.write_entry(EntryDraft(id=task, base_version=1, reason="review", body="fixed"))
    fixed = entries.add_comment(task, "fixed in v2", addresses=[second.id]).comment
    page = entries.get_comments(task, limit=10)
    assert unchanged_responses(page.comments) == {answered.id}
    assert (fixed.entry_version, page.comments[-1].verdict) == (2, "changes_requested")


def test_a_comment_on_a_goal_pins_every_descendant(entries: EntryStore) -> None:
    """Silent failure caught: a plan review that cannot later say which tasks changed."""
    goal = make(entries, "goal", "g")
    task = make(entries, "task", "t", parent=goal)
    subtask = make(entries, "task", "s", parent=task)
    entries.write_entry(EntryDraft(id=subtask, base_version=1, reason="r", body="v2"))
    outcome = entries.add_comment(goal, "plan looks right", verdict="approve")
    assert outcome.pinned == {task: 1, subtask: 2}


def test_since_comment_lists_exactly_the_changed_versions_and_links(entries: EntryStore) -> None:
    """Silent failure caught: a re-review missing an edited task, a task added after the
    review, or a removed dependency, or listing untouched tasks as changed."""
    goal = make(entries, "goal", "g")
    edited = make(entries, "task", "edited", parent=goal)
    untouched = make(entries, "task", "untouched", parent=goal)
    blocker = make(entries, "task", "blocker", parent=goal, waits_on=[])
    entries.add_entry_link(edited, "waits_on", blocker)
    review = entries.add_comment(goal, "review 1").comment.id
    entries.write_entry(EntryDraft(id=edited, base_version=1, reason="feedback", body="better"))
    added = make(entries, "task", "added after review", parent=goal)
    entries.remove_entry_link(edited, "waits_on", blocker, "not needed")
    since = entries.changes_since_comment(review)
    assert sorted((c.entry_id, c.pinned, c.current) for c in since.changed) == sorted(
        [(edited, 1, 2), (added, None, 1)]
    )
    assert sorted(since.unchanged) == sorted([goal, untouched, blocker])
    assert sorted((e.source_id, e.relation, e.target_id, e.action) for e in since.links) == sorted(
        [
            (added, "part_of", goal, "add"),
            (edited, "waits_on", blocker, "remove"),
        ]
    )


def test_a_comment_writes_no_version(entries: EntryStore) -> None:
    """Silent failure caught: a reviewer's comment making the editor's base_version stale."""
    task = make(entries, "task", "t")
    entries.add_comment(task, "looks fine")
    entries.write_entry(EntryDraft(id=task, base_version=1, reason="edit", body="b"))
    assert [v.version for v in entries.get_versions(task)] == [1, 2]
