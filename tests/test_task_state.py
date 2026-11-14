"""Links with events and derived task state, against real Dolt."""

from __future__ import annotations

import pytest

from trails import sessions
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import EntryDraft, Provenance
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
    with pytest.raises(ValidationError, match="supersedes is not a link relation"):
        entries.add_entry_link(task, "supersedes", make(entries, "task", "t3"))
    assert entries.task_graph().waits == {}


def test_claim_refusals(entries: EntryStore) -> None:
    """Silent failure caught: two sessions working one task, a note claimed as work, a claim
    made against a version the claimant never read, or a takeover of a claim the caller did
    not read (the holder plays the part of base_version for the claim itself)."""
    note = make(entries, "note", "n")
    task = make(entries, "task", "t")
    with pytest.raises(ValidationError, match="only a task is claimed"):
        entries.claim(note, 1)
    entries.write_entry(EntryDraft(id=task, base_version=1, reason="r", body="more"))
    with pytest.raises(ValidationError, match="base_version=1 is stale"):
        entries.claim(task, 1)
    held = entries.claim(task, 2)
    assert held.event is not None
    holder = held.event.session_id or held.event.harness
    assert (held.changed, held.entry.state) == (True, "claimed")
    assert entries.claim(task, 2).changed is False
    token = as_client("other-harness")
    try:
        with pytest.raises(ValidationError, match="this call named no holder") as refused:
            entries.claim(task, 2)
        assert f'retry with claim("{task}", base_version=2, holder="{holder}")' in str(
            refused.value
        )
        with pytest.raises(ValidationError, match='named holder="someone-else"'):
            entries.claim(task, 2, holder="someone-else")
        taken = entries.claim(task, 2, holder=holder)
    finally:
        sessions.CLIENT_NAME.reset(token)
    assert taken.event is not None
    assert (taken.changed, taken.event.harness) == (True, "other-harness")


def test_claiming_or_closing_a_closed_task_is_refused_with_its_state(
    entries: EntryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught (Derek's review fix 1): a second close with a different outcome
    answered with "nothing changed" and a warning, so the agent's outcome was lost without a
    refusal; a claim reopening closed work; or a refusal pointing at another operation
    (principle 5). Both are refused as overwrites, and nothing is written."""
    provenance = Provenance(harness="kiro", session_id="session-closer")
    monkeypatch.setattr(sessions, "current_provenance", lambda: provenance)
    monkeypatch.setattr("trails.entry_store.current_author", lambda: "closer")
    done = make(entries, "task", "done")
    entries.close(done, "completed", "ok")
    events_before = len(entries.get_entry(done).task_events)
    refusals = []
    for attempt in (
        lambda: entries.close(done, "failed", "oops"),
        lambda: entries.claim(done, 1),
    ):
        with pytest.raises(ValidationError) as refused:
            attempt()
        refusals.append(str(refused.value))
    for text in refusals:
        assert text.startswith(
            f"{done} is already closed completed, by closer (kiro, session session-closer) at "
        )
        assert text.endswith("; nothing was written")
        assert "reopen" not in text
    after = entries.get_entry(done)
    assert (after.outcome, len(after.task_events)) == ("completed", events_before)


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


def test_a_link_closing_a_cycle_is_refused_with_its_ids(entries: EntryStore) -> None:
    """Silent failure caught: a child stored waiting on its own ancestor, which can never
    become ready (D8); a cycle is malformed, so it is refused and nothing is written."""
    parent = make(entries, "task", "parent")
    child = make(entries, "task", "child", parent=parent)
    with pytest.raises(ValidationError) as refused:
        entries.add_entry_link(child, "waits_on", parent)
    assert str(refused.value) == (
        "these links would make a dependency cycle, so none of its tasks could become ready: "
        f"{child} -> {parent} -> {child}"
    )
    assert entries.task_graph().waits == {}


def test_an_evidence_link_made_again_records_the_targets_new_version(
    entries: EntryStore,
) -> None:
    """Silent failure caught: re-linking after reading a changed target leaving the link
    marked "written against v1, now v2" forever, or a re-link at the same version writing a
    duplicate event."""
    source = make(entries, "note", "evidence")
    target = make(entries, "note", "finding")
    entries.add_entry_link(source, "supports", target)
    assert entries.add_entry_link(source, "supports", target).changed is False
    entries.write_entry(EntryDraft(id=target, base_version=1, reason="r", body="more"))
    again = entries.add_entry_link(source, "supports", target)
    assert (again.changed, again.target_version, again.refreshed_from) == (True, 2, 1)
    [link] = entries.get_entry(source).links
    assert link.target_version == 2


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


def test_show_names_the_tasks_the_creating_session_held_claims_on(
    entries: EntryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: the fact that replaced `discovered_from` naming a task another
    session held, a task this session had already released, or a task claimed afterwards. The
    sessions are fixed here, so the result never depends on the session the suite runs in."""

    def in_session(session_id: str) -> None:
        provenance = Provenance(harness="kiro", session_id=session_id)
        monkeypatch.setattr(sessions, "current_provenance", lambda: provenance)

    in_session("session-this")
    held = make(entries, "task", "held")
    released = make(entries, "task", "released")
    later = make(entries, "task", "later")
    entries.claim(held, 1)
    entries.claim(released, 1)
    entries.release(released, "paused")
    in_session("session-other")
    theirs = make(entries, "task", "theirs")
    entries.claim(theirs, 1)
    in_session("session-this")
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


def test_a_link_closing_a_cycle_through_a_descendant_is_refused(entries: EntryStore) -> None:
    """Silent failure caught: a cycle that runs through the source's child rather than the
    source, stored without a word because only cycles through the source were checked (a
    task inherits its ancestors' waits_on targets). Both cases from the R4 review."""
    source = make(entries, "task", "S")
    child = make(entries, "task", "D", parent=source)
    make(entries, "task", "T", waits_on=[child])
    waiting = entries.task_graph().dependents(child)[0]
    with pytest.raises(ValidationError, match="dependency cycle"):
        entries.add_entry_link(source, "waits_on", waiting)

    other = make(entries, "task", "S2")
    other_child = make(entries, "task", "D2", parent=other)
    middle = make(entries, "task", "X", waits_on=[other_child])
    top = make(entries, "task", "T2", waits_on=[middle])
    with pytest.raises(ValidationError, match="dependency cycle"):
        entries.add_entry_link(other, "part_of", top)
    assert entries.task_graph().waits.get(source) is None
    assert entries.task_graph().parents.get(other) is None


def test_unlinking_an_id_that_names_no_entry_is_refused(entries: EntryStore) -> None:
    """Silent failure caught: a mistyped id in an unlink reported as "nothing changed", so
    the agent believes the link is gone when it unlinked nothing."""
    task = make(entries, "task", "t")
    with pytest.raises(ValidationError, match="no entry has id tsk-aaaaa"):
        entries.remove_entry_link(task, "waits_on", "tsk-aaaaa", "typo")


def test_a_replace_closing_a_cycle_through_a_descendant_is_refused(entries: EntryStore) -> None:
    """Silent failure caught: a replace whose moved waits_on link gives a parent a dependency
    that waits on the parent's own child, stored silently; the task stays open."""
    old = make(entries, "task", "O")
    source = make(entries, "task", "S", waits_on=[old])
    child = make(entries, "task", "D", parent=source)
    replacement = make(entries, "task", "T", waits_on=[child])
    with pytest.raises(ValidationError, match="dependency cycle"):
        entries.close(old, "superseded", "split", by=[replacement])
    assert entries.get_entry(old).state == "open"
