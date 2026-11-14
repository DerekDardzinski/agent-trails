"""The entry operations through the binder seam, against real Dolt."""

import asyncio
from typing import Any

import pytest

from trails.entry_store import EntryStore
from trails.models import ArtifactRef
from trails.ops import context
from trails.ops._entry_types import preference_section
from trails.ops.blocked import blocked
from trails.ops.changes_since_comment import changes_since_comment
from trails.ops.claim import claim
from trails.ops.close import close
from trails.ops.comment import comment
from trails.ops.create_note import create_note
from trails.ops.create_preference import create_preference
from trails.ops.create_skill import create_skill
from trails.ops.create_task import create_task
from trails.ops.link_evidence import link_evidence
from trails.ops.link_work import link_work
from trails.ops.prime import prime
from trails.ops.release import release
from trails.ops.reopen import reopen
from trails.ops.replace import replace
from trails.ops.reply import reply
from trails.ops.retire import retire
from trails.ops.review import review
from trails.ops.revise_task import revise_task
from trails.ops.search import search
from trails.ops.set_priority import set_priority
from trails.ops.set_projects import set_projects
from trails.ops.show import show
from trails.ops.unlink_work import unlink_work
from trails.present import PREFERENCE_CAP, PREFERENCE_CAP_WARNING


def run(store: EntryStore, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


CREATE = {
    "task": create_task,
    "note": create_note,
    "preference": create_preference,
    "skill": create_skill,
}


def new(store: EntryStore, kind: str, title: str, **fields: Any) -> str:
    return run(store, CREATE[kind], title=title, **fields).id


def test_prime_prints_project_preferences_first_and_warns_at_the_cap(store: EntryStore) -> None:
    """Silent failure caught: a global preference shown before the project one that overrides
    it, or the session-start page growing past what the harness hook accepts (dec-9b2ta)."""
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    old_global = new(store, "preference", "global old", body="g" * 3000)
    new_global = new(store, "preference", "global new", body="h" * 3000)
    project_pref = new(store, "preference", "project rule", body="p" * 3000, projects=["alpha"])
    overflow = new(store, "preference", "global newest", body="n" * 3000)

    result = run(store, prime, project="alpha")
    order = [(item.id, item.scope, item.full) for item in result.preferences]
    assert order == [
        (project_pref, "project", True),
        (overflow, "global", True),
        (new_global, "global", True),
        (old_global, "global", False),
    ]
    assert result.preference_cap_hit is True
    section = "\n".join(preference_section(result))
    assert len(section) <= PREFERENCE_CAP
    assert section.endswith(PREFERENCE_CAP_WARNING)
    assert f'[{old_global} · global] global old · show("{old_global}")' in section
    assert result.render_text().startswith("preferences:\n[" + project_pref)


def test_prime_preference_section_stays_under_the_cap_when_titles_alone_pass_it(
    store: EntryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review advisory note. Silent failure caught: once the title lines alone pass the cap
    (over 100 preferences at 10,000 characters), every one was still printed, so the
    session-start page grew without bound against D24. The cap is lowered to 300 so a handful
    of preferences reach that state."""
    from trails.ops import prime as prime_module

    monkeypatch.setattr(prime_module, "PREFERENCE_CAP", 300)
    ids = [new(store, "preference", f"rule {n} " + "x" * 40) for n in range(6)]
    store.add_entry_link(ids[0], "contradicts", ids[1])
    result = run(store, prime)
    section = "\n".join(preference_section(result))
    assert len(section) <= 300
    shown = [item.id for item in result.preferences]
    assert shown == list(reversed(ids))[: len(shown)]
    assert all(not item.full for item in result.preferences)
    assert result.preferences_omitted == 6 - len(shown) > 0
    assert (
        f'{result.preferences_omitted} more preferences not listed: search(kinds=["preference"])'
        in section
    )
    assert result.preference_cap_hit is True
    assert f"conflict: {ids[0]} contradicts {ids[1]}" in section


def test_prime_preference_conflicts_are_cut_at_the_cap_with_the_call_that_lists_them(
    store: EntryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: conflict lines pushing the section past the cap after every
    preference has been cut, with no way to list the conflicts left out."""
    from trails.ops import prime as prime_module

    monkeypatch.setattr(prime_module, "PREFERENCE_CAP", 330)
    ids = [new(store, "preference", f"r{n}") for n in range(8)]
    for n in range(0, 8, 2):
        store.add_entry_link(ids[n], "contradicts", ids[n + 1])
    result = run(store, prime)
    section = "\n".join(preference_section(result))
    assert len(section) <= 330
    assert (len(result.preference_conflicts), result.preference_conflicts_omitted) == (1, 3)
    assert section.endswith(
        f"{result.preference_conflicts_omitted} more conflicts not listed: "
        'search(kinds=["preference"], contradicted=true)'
    )


def test_prime_index_lists_projects_and_counts_unscoped(store: EntryStore) -> None:
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    parent = new(store, "task", "g", projects=["alpha"])
    new(store, "task", "child", parent=parent, projects=["alpha"])
    new(store, "task", "a task with no children", projects=["alpha"])
    new(store, "note", "loose")
    result = run(store, prime)
    assert [(row.name, row.open_parents) for row in result.projects] == [("alpha", 1)]
    assert result.unscoped == 1


def test_a_hit_shows_supporting_and_contradicting_entries_as_plain_links(
    store: EntryStore,
) -> None:
    """Silent failure caught: a note shown with a verdict-like label (observation, assertion)
    derived by trails, or a contradiction missing from the hit, instead of the stored links."""
    measured = new(
        store,
        "note",
        "p99 fell 12%",
        artifacts=[ArtifactRef(location="s3://bench/run1.csv")],
    )
    backer = new(store, "note", "load test notes")
    rival = new(store, "note", "p99 did not fall")
    run(store, link_evidence, source=backer, relation="supports", target=measured)
    run(store, link_evidence, source=rival, relation="contradicts", target=measured)
    [hit] = [item for item in run(store, search, kinds=["note"]).hits if item.id == measured]
    assert hit.evidence == [f"supported by {backer}", f"contradicted by {rival}"]
    text = run(store, search, kinds=["note"]).render_text()
    assert "observation" not in text
    assert "standing" not in text


def _graph_ids(result: Any) -> list[str]:
    return [line.id for line in result.graph]


def test_every_task_changing_response_redraws_the_affected_graph(store: EntryStore) -> None:
    """Silent failure caught: a missing or reversed link that nothing shows, because the
    response to the change that made it named only the entry changed (dec-o0gww)."""
    goal = run(store, create_task, title="g")
    assert _graph_ids(goal) == [goal.id]
    first = run(store, create_task, title="a", parent=goal.id)
    assert _graph_ids(first) == [goal.id, first.id]
    second = run(store, create_task, title="b", parent=goal.id, waits_on=[first.id])
    assert _graph_ids(second) == [goal.id, second.id, first.id]
    assert [line.state for line in second.graph] == [
        "children: 2 open",
        f"waits on {first.id} (open)",
        "ready",
    ]
    linked = run(store, link_work, source=second.id, relation="part_of", target=goal.id)
    assert _graph_ids(linked) == [goal.id, second.id, first.id]
    removed = run(
        store, unlink_work, source=second.id, relation="waits_on", target=first.id, reason="x"
    )
    assert _graph_ids(removed) == [goal.id, second.id, first.id]
    claimed = run(store, claim, id=first.id, base_version=1)
    assert _graph_ids(claimed) == [goal.id, first.id]
    released = run(store, release, id=first.id, reason="later")
    assert _graph_ids(released) == [goal.id, first.id]
    closed = run(store, close, id=first.id, outcome="completed", summary="done")
    assert _graph_ids(closed) == [goal.id, first.id]
    reopened = run(store, reopen, id=first.id, reason="regressed")
    assert _graph_ids(reopened) == [goal.id, first.id]
    assert all(text in closed.render_text() for text in ("graph:", f"{first.id}  task  [closed"))


def test_close_names_dependents_with_their_new_state(store: EntryStore) -> None:
    """Silent failure caught: closing a task without seeing which work it just unblocked or
    left needing a new plan."""
    first = new(store, "task", "a")
    waiting = new(store, "task", "b", waits_on=[first])
    closed = run(store, close, id=first, outcome="dropped", summary="not needed")
    [dependent] = closed.dependents
    assert (dependent.id, dependent.state) == (waiting, f"waits on {first} (closed dropped)")
    held_up = run(store, blocked)
    assert [(item.id, item.waits_on) for item in held_up.blocked] == [
        (waiting, [f"{first} (closed dropped)"])
    ]


def test_search_ranks_every_kind_the_same(store: EntryStore) -> None:
    """Silent failure caught: a fixed penalty pushing notes below other kinds, a judgment of
    worth the store does not make. With equal scores the newer entry comes first."""
    preference = new(store, "preference", "retry budget exhausted under load")
    note = new(store, "note", "retry budget exhausted under load")
    assert [hit.id for hit in run(store, search, query="retry budget exhausted").hits] == [
        note,
        preference,
    ]


def test_search_status_selects_by_the_latest_task_event(store: EntryStore) -> None:
    """Silent failure caught: a reopened task still listed as closed, or a released one as
    claimed."""
    reopened = new(store, "task", "reopened")
    released = new(store, "task", "released")
    failed = new(store, "task", "failed")
    run(store, close, id=reopened, outcome="completed", summary="x")
    run(store, reopen, id=reopened, reason="y")
    run(store, claim, id=released, base_version=1)
    run(store, release, id=released, reason="z")
    run(store, close, id=failed, outcome="failed", summary="w")

    def ids(status: str) -> set[str]:
        return {hit.id for hit in run(store, search, status=status).hits}

    assert ids("open") == {reopened, released}
    assert ids("claimed") == set()
    assert ids("closed") == {failed}
    assert ids("failed") == {failed}


def test_changes_since_comment_lists_what_changed_after_a_review(store: EntryStore) -> None:
    """Silent failure caught: a returning reviewer not seeing a task under the reviewed one
    that moved on, or being able to ask a different entry than the one the comment is on
    (the comment id alone names the entry, so that call cannot be written)."""
    goal = new(store, "task", "g")
    task = new(store, "task", "t", parent=goal)
    verdict = run(store, review, id=goal, verdict="approve")
    run(store, revise_task, id=task, base_version=1, reason="feedback", body="more detail")
    since = run(store, changes_since_comment, comment=verdict.comment_id)
    assert (since.entry_id, since.changed, since.links) == (goal, [f"{task} t: v1 -> v2"], [])


def test_reply_closes_the_comments_it_answers_on_one_entry(store: EntryStore) -> None:
    """Silent failure caught: a reply closing a comment on another entry, or feedback left
    open after it was answered."""
    from trails.errors import ValidationError

    first = new(store, "task", "a")
    second = new(store, "task", "b")
    question = run(store, comment, id=first, comment="why this order?").comment_id
    other = run(store, comment, id=second, comment="and this one?").comment_id
    with pytest.raises(ValidationError, match=f"{other} is not a comment on {first}"):
        run(store, reply, addresses=[question, other], comment="answered")
    answered = run(store, reply, addresses=[question], comment="dependency order")
    assert answered.addresses == [question]
    assert run(store, show, id=first).entries[0].comment_open == 0
    assert run(store, show, id=second).entries[0].comment_open == 1


def test_retire_takes_knowledge_ids_and_hides_them_from_search(store: EntryStore) -> None:
    """Silent failure caught: a task retired (it ends only by close or replace, so a retire
    would hide it from its dependents' facts), or a retired note still listed by search."""
    from trails.errors import ValidationError

    task = new(store, "task", "a")
    note = new(store, "note", "wrong finding")
    with pytest.raises(ValidationError, match="retire takes a note, preference or skill"):
        store.retire_entry(task, "wrong plan")
    run(store, retire, id=note, reason="wrong")
    assert run(store, search, query="", kinds=["note"]).total == 0


def test_set_projects_moves_an_entry_between_projects(store: EntryStore) -> None:
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    store.write_project("beta", host="h", paths=[], remove_paths=[])
    note = new(store, "note", "n", projects=["alpha"])
    result = run(store, set_projects, id=note, add=["beta"], remove=["alpha"], reason="moved")
    assert [ref.name for ref in result.projects] == ["beta"]
    assert [ref.name for ref in store.get_entry(note).projects] == ["beta"]


def test_a_secret_in_a_set_projects_reason_is_warned_about(store: EntryStore) -> None:
    """Silent failure caught: a secret written into a membership reason, which is stored for
    good, with no warning, although every other reason is scanned."""
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    note = new(store, "note", "n")
    result = run(
        store, set_projects, id=note, add=["alpha"], reason="key AKIAIOSFODNN7EXAMPLE moved"
    )
    assert result.warnings == [
        "possible secret: AWSKeyDetector in reason line 1; tell the person, because removing "
        "it needs a manual database-history rewrite"
    ]
    assert "warning: possible secret: AWSKeyDetector in reason line 1" in result.render_text()


@pytest.mark.parametrize("kind", ["note", "preference", "skill"])
def test_a_non_work_write_draws_no_graph(store: EntryStore, kind: str) -> None:
    assert run(store, CREATE[kind], title="x").graph == []


def test_a_script_artifact_in_a_temporary_directory_is_warned_about(
    store: EntryStore, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: an entry pointing at a script the operating system will delete,
    which trails holds only by reference."""
    from trails import locations

    script = tmp_path / "analysis.py"
    script.write_text("print(1)\n")
    monkeypatch.setattr(locations, "temporary_roots", lambda: (tmp_path.resolve(),))
    result = run(store, create_note, title="x", artifacts=[ArtifactRef(location=str(script))])
    [warning] = result.warnings
    assert "is a script in the temporary directory" in warning
    assert (
        f'revise_note("{result.id}", base_version=1, reason="...", '
        'repos=[{"path": "<repository>"}])' in warning
    )


AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_a_replace_that_would_close_a_cycle_is_refused(store: EntryStore) -> None:
    """Silent failure caught: a replace moving a
    child onto its own parent, so the child can never become ready; the move is malformed,
    so the replace is refused and the task stays open."""
    from trails.errors import ValidationError

    parent = new(store, "task", "parent")
    old = new(store, "task", "old")
    child = new(store, "task", "child", parent=parent, waits_on=[old])
    with pytest.raises(ValidationError, match=f"{child} -> {parent} -> {child}"):
        run(store, replace, id=old, by=[parent], reason="moved")
    assert store.get_entry(old).state == "open"


def test_replace_moves_dependents_and_shows_them(store: EntryStore) -> None:
    """Silent failure caught: a dependent missing from the response to the replace that
    moved its waits_on link, so the agent cannot see what now waits on the replacements."""
    old = new(store, "task", "old")
    first = new(store, "task", "part a")
    waiting = new(store, "task", "uses it", waits_on=[old])
    replaced = run(store, replace, id=old, by=[first], reason="split")
    assert replaced.state == "closed superseded"
    assert [(m.source_id, m.to_id) for m in replaced.moved] == [(waiting, first)]
    assert [(line.id, line.state) for line in replaced.dependents] == [
        (waiting, f"waits on {first} (open)")
    ]


def test_every_free_text_write_is_scanned_for_secrets(store: EntryStore) -> None:
    """Review finding 2. Silent failure caught: a key pasted into a comment, a close summary or
    a reason stored without the warning that tells the person."""
    task = new(store, "task", "t")
    note = new(store, "note", "n")
    other = new(store, "note", "o")
    feedback = run(store, comment, id=task, comment=f"aws_key = {AWS_KEY}")
    linked = run(
        store, link_evidence, source=note, relation="supports", target=other, reason=AWS_KEY
    )
    closed = run(store, close, id=task, outcome="completed", summary=f"used {AWS_KEY}")
    assert any("AWSKeyDetector in comment" in w for w in feedback.warnings)
    assert any("AWSKeyDetector in reason" in w for w in linked.warnings)
    found = {w.split(" in ")[1].split(" line")[0] for w in closed.warnings if "AWSKeyDetector" in w}
    assert found == {"summary"}
    assert all(AWS_KEY not in text for text in (feedback.render_text(), closed.render_text()))


def test_a_stale_claim_names_the_claim_retry(store: EntryStore) -> None:
    """Review finding 3. Silent failure caught: a refused claim telling the agent to write a
    version, which never claims."""
    from trails.errors import ValidationError

    task = new(store, "task", "t")
    with pytest.raises(ValidationError) as refused:
        run(store, claim, id=task, base_version=0)
    assert f'retry with claim("{task}", base_version=1)' in str(refused.value)
    assert "write(" not in str(refused.value)


def test_scoped_prime_bounds_its_lists(store: EntryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding 5. Silent failure caught: prime growing with every contradicted entry
    and open parent task until the session-start page is dropped. The limit is lowered to 2 so the
    test writes a handful of entries rather than dozens."""
    from trails.ops import prime as prime_module

    monkeypatch.setattr(prime_module, "LIST_LIMIT", 2)
    limit = 2
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    rival = new(store, "note", "rival", projects=["alpha"])
    for n in range(limit + 2):
        target = new(store, "note", f"c{n}", projects=["alpha"])
        store.add_entry_link(rival, "contradicts", target)
        parent = new(store, "task", f"g{n}", projects=["alpha"])
        new(store, "task", f"child {n}", parent=parent, projects=["alpha"])
    result = run(store, prime, project="alpha")
    assert (len(result.contradicted), result.contradicted_total) == (limit, limit + 2)
    assert (len(result.parents), result.parent_total) == (limit, limit + 2)


def test_search_contradicted_lists_only_currently_contradicted_entries(
    store: EntryStore,
) -> None:
    """Silent failure caught: prime's `more:` call for hidden contradicted entries listing every
    note, or listing a claim whose contradiction was unlinked or whose contradicting entry was
    retired, so the agent cannot find the corrections prime counted."""
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    rival = new(store, "note", "rival", projects=["alpha"])
    retired_rival = new(store, "note", "retired rival", projects=["alpha"])
    contradicted = new(store, "note", "contradicted", projects=["alpha"])
    unlinked = new(store, "note", "was contradicted", projects=["alpha"])
    by_retired = new(store, "note", "contradicted by a retired claim", projects=["alpha"])
    new(store, "note", "never contradicted", projects=["alpha"])
    store.add_entry_link(rival, "contradicts", contradicted)
    store.add_entry_link(rival, "contradicts", unlinked)
    store.remove_entry_link(rival, "contradicts", unlinked, "rival was about something else")
    store.add_entry_link(retired_rival, "contradicts", by_retired)
    run(store, retire, id=retired_rival, reason="wrong")
    result = run(store, search, kinds=["note"], projects=["alpha"], contradicted=True)
    assert [hit.id for hit in result.hits] == [contradicted]


def test_create_task_refuses_a_parent_and_dependency_that_close_a_cycle(
    store: EntryStore,
) -> None:
    """Silent failure caught: a new task stored waiting on its own grandparent, which needs
    the new task's parent, which needs the new task: none of the three can ever become ready.
    Creation can close a cycle although a new task has no incoming links."""
    from trails.errors import ValidationError

    grandparent = new(store, "task", "g")
    parent = new(store, "task", "p", parent=grandparent)
    with pytest.raises(ValidationError, match="dependency cycle") as refused:
        run(store, create_task, title="x", parent=parent, waits_on=[grandparent])
    assert f"the new task -> {grandparent} -> {parent} -> the new task" in str(refused.value)
    assert store.query("SELECT COUNT(*) AS n FROM entry") == [{"n": 2}]


def test_set_priority_writes_a_version_and_clears_with_null(store: EntryStore) -> None:
    """Silent failure caught: null read as "keep the priority", so a priority can never be
    removed, or a priority change written without a version and a reason."""
    task = new(store, "task", "t", priority=1)
    raised = run(store, set_priority, id=task, base_version=1, priority=0, reason="urgent")
    cleared = run(store, set_priority, id=task, base_version=2, priority=None, reason="parked")
    versions = store.get_versions(task)
    assert [(v.version, v.priority, v.reason) for v in versions] == [
        (1, 1, None),
        (2, 0, "urgent"),
        (3, None, "parked"),
    ]
    assert (raised.version, cleared.version) == (2, 3)


def test_a_temporary_path_in_a_task_body_asks_only_for_a_body_change(
    store: EntryStore, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: a warning on a task telling the agent to pass repos and
    artifacts, which revise_task does not take, so the fix it names cannot be made."""
    from trails import locations

    monkeypatch.setattr(locations, "temporary_roots", lambda: (tmp_path.resolve(),))
    result = run(store, create_task, title="w", body=f"script at {tmp_path}/foo/run.py")
    [warning] = result.warnings
    assert "repos" not in warning
    assert "artifacts" not in warning
    assert warning.endswith(
        f'write the new locations in the body: revise_task("{result.id}", base_version=1, '
        'reason="...", body=...)'
    )


def test_merge_retires_a_duplicate_naming_the_entry_it_duplicates(store: EntryStore) -> None:
    """Silent failure caught: a merged duplicate indistinguishable from an entry retired as
    wrong, or a merge across kinds that would drop a preference out of session start."""
    from trails.errors import ValidationError
    from trails.ops.merge import merge

    kept = new(store, "skill", "deploy with the canary")
    duplicate = new(store, "skill", "canary deploys")
    preference = new(store, "preference", "use canaries")
    with pytest.raises(ValidationError, match="a merge joins two entries of one kind"):
        run(store, merge, id=preference, into=kept, reason="same")
    run(store, merge, id=duplicate, into=kept, reason="same procedure")
    shown = run(store, show, id=duplicate).entries[0]
    assert shown.retired_reason == f"same procedure (duplicate of {kept})"


def test_changes_lists_every_write_in_the_window_for_one_session(
    store: EntryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: the consolidation pass missing a session's close summary,
    comment or link, or seeing another session's writes as this one's."""
    from datetime import UTC, datetime

    from trails import sessions
    from trails.ops.changes import changes

    start = datetime.now(UTC)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "dreamed-session")
    token = sessions.CLIENT_NAME.set("claude-code")
    try:
        task = new(store, "task", "t")
        run(store, comment, id=task, comment="why?")
        run(store, close, id=task, outcome="completed", summary="done")
    finally:
        sessions.CLIENT_NAME.reset(token)
    new(store, "note", "another session's note")
    feed = run(store, changes, since=start, session="dreamed-session")
    assert [(item.entry_id, item.kind) for item in feed.changes] == [
        (task, "version"),
        (task, "comment"),
        (task, "task"),
    ]
    assert feed.changes[2].text == "close completed · done"
    assert feed.total == 3
