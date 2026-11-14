"""Links with events and derived task state, against real Dolt."""

from __future__ import annotations

import pytest

from trails import sessions
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import EntryDraft, FollowUp


def make(entries: EntryStore, kind: str, title: str, **fields: object) -> str:
    return entries.write_entry(EntryDraft(kind=kind, title=title, **fields)).entry.id  # ty: ignore[invalid-argument-type]


def as_client(name: str):
    """Make writes come from a different harness, which is a different claimant."""
    return sessions.CLIENT_NAME.set(name)


def test_dependent_of_a_dropped_task_keeps_waiting_and_needs_replanning(
    entries: EntryStore,
) -> None:
    """Silent failure caught: dropping a task unblocking work that needed it done."""
    first = make(entries, "task", "build parser")
    second = make(entries, "task", "use parser", waits_on=[first])
    entries.close(first, "dropped", "not needed after all")
    graph = entries.task_graph()
    assert graph.is_ready(second) is False
    assert graph.unfinished_targets(second) == [first]
    assert graph.needs_replanning() == [second]


def test_parent_with_open_child_is_not_ready_and_a_block_on_parent_holds_the_child(
    entries: EntryStore,
) -> None:
    """Silent failure caught: a parent task offered as ready work while its children are open,
    and a child offered as ready although its parent waits on unfinished work."""
    blocker = make(entries, "task", "get access")
    parent = make(entries, "task", "migrate", waits_on=[blocker])
    child = make(entries, "task", "copy tables", parent=parent)
    graph = entries.task_graph()
    assert graph.is_ready(parent) is False
    assert graph.open_children(parent) == [child]
    assert graph.is_ready(child) is False
    assert graph.unfinished_targets(child) == [blocker]
    entries.close(blocker, "completed", "granted")
    assert entries.task_graph().ready() == [child]


def test_waiting_only_on_a_note_is_ready_and_warned(entries: EntryStore) -> None:
    """Silent failure caught: a task stuck forever on a note, which can never close."""
    note = make(entries, "note", "scratch")
    outcome = entries.write_entry(EntryDraft(kind="task", title="t", waits_on=[note]))
    assert "only a goal or task counts as a dependency" in outcome.warnings[0]
    assert entries.task_graph().ready() == [outcome.entry.id]


def test_claim_refusals(entries: EntryStore) -> None:
    """Silent failure caught: two sessions working one task, a goal claimed as work, or a
    claim made against a version the claimant never read."""
    goal = make(entries, "goal", "g")
    task = make(entries, "task", "t")
    with pytest.raises(ValidationError, match="only a task is claimed"):
        entries.claim(goal, 1)
    entries.write_entry(EntryDraft(id=task, base_version=1, reason="r", body="more"))
    with pytest.raises(ValidationError, match="base_version=1 is stale"):
        entries.claim(task, 1)
    held = entries.claim(task, 2)
    assert (held.changed, held.entry.state) == (True, "claimed")
    assert entries.claim(task, 2).changed is False
    token = as_client("other-harness")
    try:
        with pytest.raises(ValidationError, match="take_over_reason"):
            entries.claim(task, 2)
        taken = entries.claim(task, 2, take_over_reason="the first session ended")
    finally:
        sessions.CLIENT_NAME.reset(token)
    assert taken.event is not None
    assert (taken.event.harness, taken.event.reason) == ("other-harness", "the first session ended")
    done = make(entries, "task", "done")
    entries.close(done, "completed", "ok")
    with pytest.raises(ValidationError, match="is closed completed"):
        entries.claim(done, 1)


def test_supersede_moves_every_dependent_onto_every_replacement(entries: EntryStore) -> None:
    """Silent failure caught: a dependent left waiting on a superseded task, or waiting on
    only one of its replacements."""
    old = make(entries, "task", "one big change")
    first_dependent = make(entries, "task", "d1", waits_on=[old])
    second_dependent = make(entries, "task", "d2", waits_on=[old])
    part_a = make(entries, "task", "part a")
    part_b = make(entries, "task", "part b")
    with pytest.raises(ValidationError, match="needs by"):
        entries.close(old, "superseded", "split")
    outcome = entries.close(old, "superseded", "split in two", by=[part_a, part_b])
    assert sorted((m.source_id, m.to_id) for m in outcome.moved) == sorted(
        [
            (first_dependent, part_a),
            (first_dependent, part_b),
            (second_dependent, part_a),
            (second_dependent, part_b),
        ]
    )
    graph = entries.task_graph()
    assert sorted(graph.waits[first_dependent]) == sorted([part_a, part_b])
    assert sorted(graph.waits[second_dependent]) == sorted([part_a, part_b])
    assert [link.relation for link in entries.get_entry(part_a).links] == ["supersedes"]
    entries.reopen(old, "the split was premature")
    after = entries.task_graph()
    assert sorted(after.waits[first_dependent]) == sorted([part_a, part_b])
    assert after.dependents(old) == []


def test_ready_to_close_excludes_a_leaf_and_a_parent_with_a_failed_child(
    entries: EntryStore,
) -> None:
    """Silent failure caught: a goal reported done while one of its tasks failed."""
    goal = make(entries, "goal", "ship")
    good = make(entries, "task", "a", parent=goal)
    bad = make(entries, "task", "b", parent=goal)
    leaf = make(entries, "task", "leaf")
    entries.close(good, "completed", "ok")
    entries.close(bad, "failed", "flaky")
    entries.close(leaf, "dropped", "n/a")
    graph = entries.task_graph()
    assert graph.is_ready_to_close(goal) is False
    assert graph.needs_decision(goal) is True
    assert graph.is_ready_to_close(make(entries, "task", "another leaf")) is False
    other_goal = make(entries, "goal", "g2")
    done = make(entries, "task", "c", parent=other_goal)
    gone = make(entries, "task", "d", parent=other_goal)
    entries.close(done, "completed", "ok")
    entries.close(gone, "dropped", "no longer needed")
    assert entries.task_graph().is_ready_to_close(other_goal) is True


def test_a_cycle_warns_with_its_ids(entries: EntryStore) -> None:
    """Silent failure caught: a child waiting on its own ancestor, which never becomes ready
    and nothing says why (D8)."""
    parent = make(entries, "task", "parent")
    child = make(entries, "task", "child", parent=parent)
    outcome = entries.add_entry_link(child, "waits_on", parent)
    assert outcome.warnings == [
        f"dependency cycle: {child} -> {parent} -> {child}; none of these can become ready "
        "until one link in the cycle is removed with unlink"
    ]
    assert entries.add_entry_link(child, "waits_on", parent).changed is False


def test_ready_sorts_by_priority_then_age(entries: EntryStore) -> None:
    """Silent failure caught: the most urgent work not at the head of the queue."""
    unprioritized = make(entries, "task", "none")
    low = make(entries, "task", "low", priority=3)
    first_high = make(entries, "task", "high 1", priority=0)
    late_urgent = make(entries, "task", "high 2", priority=0)
    assert entries.task_graph().ready() == [first_high, late_urgent, low, unprioritized]


def test_unlink_needs_a_reason_and_restores_readiness(entries: EntryStore) -> None:
    """Silent failure caught: a dependency removed with no record of why."""
    blocker = make(entries, "task", "b")
    task = make(entries, "task", "t", waits_on=[blocker])
    with pytest.raises(ValidationError, match="unlink needs a reason"):
        entries.remove_entry_link(task, "waits_on", blocker, " ")
    entries.remove_entry_link(task, "waits_on", blocker, "not actually needed")
    assert entries.task_graph().is_ready(task) is True
    events = [(e.action, e.reason) for e in entries.link_events([task])]
    assert events == [("add", None), ("remove", "not actually needed")]


def test_close_creates_follow_ups_under_the_parent(entries: EntryStore) -> None:
    """Silent failure caught: a follow-up found while closing lost outside the plan."""
    goal = make(entries, "goal", "g")
    task = make(entries, "task", "t", parent=goal)
    outcome = entries.close(
        task, "completed", "done; found a bug", follow_ups=[FollowUp(title="fix bug", priority=1)]
    )
    [follow] = outcome.follow_ups
    created = entries.get_entry(follow)
    assert (created.kind, created.current.title, created.current.priority) == ("task", "fix bug", 1)
    assert sorted((link.relation, link.target_id) for link in created.links) == sorted(
        [("discovered_from", task), ("part_of", goal)]
    )


def test_closing_a_parent_with_open_children_warns(entries: EntryStore) -> None:
    goal = make(entries, "goal", "g")
    child = make(entries, "task", "t", parent=goal)
    outcome = entries.close(goal, "dropped", "deprioritized")
    assert outcome.warnings == [
        f"{goal} was closed with children not closed: {child}; their state is unchanged"
    ]
    assert entries.task_graph().is_ready(child) is True
