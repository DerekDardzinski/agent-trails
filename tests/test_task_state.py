"""Links with events and derived task state, against real Dolt."""

from __future__ import annotations

import pytest

from trails import sessions
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import EntryDraft
from trails.present import node_state


def make(entries: EntryStore, kind: str, title: str, **fields: object) -> str:
    return entries.write_entry(EntryDraft(kind=kind, title=title, **fields)).entry.id  # ty: ignore[invalid-argument-type]


def as_client(name: str):
    """Make writes come from a different harness, which is a different claimant."""
    return sessions.CLIENT_NAME.set(name)


def test_dependent_of_a_dropped_task_keeps_waiting_and_shows_the_dropped_target(
    entries: EntryStore,
) -> None:
    """Silent failure caught: dropping a task unblocking work that needed it done, or the
    waiting task's state hiding that its target was dropped."""
    first = make(entries, "task", "build parser")
    second = make(entries, "task", "use parser", waits_on=[first])
    entries.close(first, "dropped", "not needed after all")
    graph = entries.task_graph()
    assert graph.is_ready(second) is False
    assert graph.blocked() == [second]
    assert node_state(graph, second) == f"waits on {first} (closed dropped)"


def test_a_waits_on_added_after_its_target_was_superseded_is_stored_and_shown_blocked(
    entries: EntryStore,
) -> None:
    """Silent failure caught: a task that waits on a task already closed `superseded` waits
    forever with nothing showing why. The supersede moved only the links that existed then;
    the late link is stored as given (dec-d1krx item 3) and shown as a fact."""
    old = make(entries, "task", "old parser")
    new = make(entries, "task", "new parser")
    entries.close(old, "superseded", "rewritten", by=[new])
    late = make(entries, "task", "use parser")
    assert entries.add_entry_link(late, "waits_on", old).changed is True
    graph = entries.task_graph()
    assert graph.is_ready(late) is False
    assert graph.blocked() == [late]
    assert node_state(graph, late) == f"waits on {old} (closed superseded)"


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
    assert node_state(graph, child) == f"waits on {blocker} through {parent} (open)"
    entries.close(blocker, "completed", "granted")
    assert entries.task_graph().ready() == [child]


def test_work_and_evidence_links_refuse_the_wrong_kinds(entries: EntryStore) -> None:
    """Silent failure caught: a task stored waiting on a note, which can never close, or a
    task stored as evidence; both are malformed and refused rather than warned about."""
    note = make(entries, "note", "scratch")
    task = make(entries, "task", "t")
    with pytest.raises(ValidationError, match=f"waits_on joins task entries; the target {note}"):
        entries.write_entry(EntryDraft(kind="task", title="t2", waits_on=[note]))
    with pytest.raises(ValidationError, match=f"the source {task} is a task"):
        entries.add_entry_link(task, "supports", note)
    with pytest.raises(ValidationError, match="supersedes is written when a task is closed"):
        entries.add_entry_link(task, "supersedes", make(entries, "task", "t3"))
    assert entries.task_graph().waits == {}


def test_claim_refusals(entries: EntryStore) -> None:
    """Silent failure caught: two sessions working one task, a note claimed as work, or a
    claim made against a version the claimant never read."""
    note = make(entries, "note", "n")
    task = make(entries, "task", "t")
    with pytest.raises(ValidationError, match="only a task is claimed"):
        entries.claim(note, 1)
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


def test_a_parent_shows_how_its_children_ended_as_counts(entries: EntryStore) -> None:
    """Silent failure caught: a parent whose children all closed, one of them failed, shown
    with a derived verdict ("ready to close") instead of what its children recorded."""
    parent = make(entries, "task", "ship")
    good = make(entries, "task", "a", parent=parent)
    bad = make(entries, "task", "b", parent=parent)
    make(entries, "task", "c", parent=parent)
    entries.close(good, "completed", "ok")
    entries.close(bad, "failed", "flaky")
    assert node_state(entries.task_graph(), parent) == "children: 1 completed, 1 failed, 1 open"


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


def test_closing_a_parent_with_open_children_stores_it_without_a_warning(
    entries: EntryStore,
) -> None:
    """Silent failure caught: an in-store warning (principle 7 allows warnings only for what
    happens outside the store) on a parent closed before its children; their state shows it."""
    parent = make(entries, "task", "g")
    child = make(entries, "task", "t", parent=parent)
    outcome = entries.close(parent, "dropped", "deprioritized")
    assert (outcome.changed, outcome.warnings) == (True, [])
    assert entries.task_graph().is_ready(child) is True


def test_show_names_the_tasks_the_creating_session_held_claims_on(entries: EntryStore) -> None:
    """Silent failure caught: the fact that replaced `discovered_from` naming a task another
    session held, a task this session had already released, or a task claimed afterwards."""
    held = make(entries, "task", "held")
    released = make(entries, "task", "released")
    later = make(entries, "task", "later")
    entries.claim(held, 1)
    entries.claim(released, 1)
    entries.release(released, "paused")
    token = as_client("other-harness")
    try:
        theirs = make(entries, "task", "theirs")
        entries.claim(theirs, 1)
    finally:
        sessions.CLIENT_NAME.reset(token)
    found = make(entries, "note", "found while working")
    entries.claim(later, 1)
    assert entries.claims_held_at_creation([found, held]) == {found: [held], held: []}


def test_blocked_lists_waiting_tasks_but_not_a_parent_with_open_children(
    entries: EntryStore,
) -> None:
    """Silent failure caught: `blocked` hiding a child held up by its parent's dependency, or
    listing a parent whose only open work is its own children, which is not blocked."""
    blocker = make(entries, "task", "get access")
    parent = make(entries, "task", "migrate", waits_on=[blocker])
    child = make(entries, "task", "copy tables", parent=parent)
    other_parent = make(entries, "task", "write docs")
    make(entries, "task", "chapter one", parent=other_parent)
    graph = entries.task_graph()
    assert graph.blocked() == [parent, child]
    assert graph.waiting_on(child) == [(blocker, parent)]
    assert graph.is_ready(other_parent) is False
