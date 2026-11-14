"""The entry operations through the binder seam, against real Dolt."""

import asyncio
from typing import Any

import pytest

from trails.entry_store import EntryStore
from trails.models import ArtifactRef
from trails.ops import context
from trails.ops._entry_types import preference_section
from trails.ops.add_comment import add_comment
from trails.ops.claim import claim
from trails.ops.close import close
from trails.ops.link import link
from trails.ops.prime import prime
from trails.ops.ready import ready
from trails.ops.release import release
from trails.ops.reopen import reopen
from trails.ops.retire import retire
from trails.ops.search import search
from trails.ops.set_projects import set_projects
from trails.ops.show import show
from trails.ops.unlink import unlink
from trails.ops.write import write
from trails.present import PREFERENCE_CAP, PREFERENCE_CAP_WARNING


def run(store: EntryStore, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def new(store: EntryStore, kind: str, title: str, **fields: Any) -> str:
    return run(store, write, kind=kind, title=title, **fields).id


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


def test_prime_index_lists_projects_and_counts_unscoped(store: EntryStore) -> None:
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    new(store, "goal", "g", projects=["alpha"])
    new(store, "note", "loose")
    result = run(store, prime)
    assert [(row.name, row.open_goals) for row in result.projects] == [("alpha", 1)]
    assert result.unscoped == 1


def test_a_note_hit_names_the_entries_that_use_it_as_evidence(store: EntryStore) -> None:
    """Silent failure caught: a note that already backs a claim looking like stray scratch,
    so it gets re-derived or deleted by hand (dec-ix8zd)."""
    note = new(store, "note", "Kafka lag spikes at deploy", body="observed lag spikes")
    claim_id = new(store, "claim", "Deploys cause lag")
    run(store, link, source=claim_id, relation="supports", target=note)
    found = run(store, search, kinds=["note"])
    [hit] = found.hits
    assert hit.evidence_for == [f"{claim_id} supports"]
    shown = run(store, show, id=note).entries[0]
    assert shown.evidence_for == [f"{claim_id} supports"]


def test_claim_standing_shows_every_word_together(store: EntryStore) -> None:
    """Silent failure caught: a contradicted observation shown only as supported, or a claim
    with nothing behind it shown as an observation."""
    measured = new(
        store,
        "claim",
        "p99 fell 12%",
        artifacts=[ArtifactRef(location="s3://bench/run1.csv")],
    )
    bare = new(store, "claim", "the cache is the cause")
    backer = new(store, "note", "load test notes")
    rival = new(store, "claim", "p99 did not fall")
    run(store, link, source=backer, relation="supports", target=measured)
    run(store, link, source=rival, relation="contradicts", target=measured)
    shown = {item.id: item.standing for item in run(store, show, id=[measured, bare]).entries}
    assert shown == {
        measured: ["observation", f"supported by {backer}", f"contradicted by {rival}"],
        bare: ["assertion"],
    }


def _graph_ids(result: Any) -> list[str]:
    return [line.id for line in result.graph]


def test_every_task_changing_response_redraws_the_affected_graph(store: EntryStore) -> None:
    """Silent failure caught: a missing or reversed link that nothing shows, because the
    response to the change that made it named only the entry changed (dec-o0gww)."""
    goal = run(store, write, kind="goal", title="g")
    assert _graph_ids(goal) == [goal.id]
    first = run(store, write, kind="task", title="a", parent=goal.id)
    assert _graph_ids(first) == [goal.id, first.id]
    second = run(store, write, kind="task", title="b", parent=goal.id, waits_on=[first.id])
    assert _graph_ids(second) == [goal.id, second.id, first.id]
    assert [line.state for line in second.graph] == [
        "2 of 2 children not closed",
        f"waiting on {first.id}",
        "ready",
    ]
    linked = run(store, link, source=second.id, relation="part_of", target=goal.id)
    assert _graph_ids(linked) == [goal.id, second.id, first.id]
    removed = run(store, unlink, source=second.id, relation="waits_on", target=first.id, reason="x")
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
    assert (dependent.id, dependent.state) == (
        waiting,
        f"needs replanning: waits on {first} (closed dropped)",
    )
    queue = run(store, ready)
    assert [line.id for line in queue.needs_replanning] == [waiting]


def test_search_ranks_a_note_below_a_claim_with_the_same_words(store: EntryStore) -> None:
    """Silent failure caught: scratch notes crowding out the claim distilled from them (D5)."""
    note = new(store, "note", "retry budget exhausted under load")
    claim_id = new(store, "claim", "retry budget exhausted under load")
    assert [hit.id for hit in run(store, search, query="retry budget exhausted").hits] == [
        claim_id,
        note,
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


def test_show_since_comment_lists_what_changed_after_a_review(store: EntryStore) -> None:
    goal = new(store, "goal", "g")
    task = new(store, "task", "t", parent=goal)
    review = run(store, add_comment, id=goal, comment="looks right", verdict="approve")
    run(store, write, id=task, base_version=1, reason="feedback", body="more detail")
    shown = run(store, show, id=goal, since_comment=review.comment_id).entries[0]
    assert shown.since == [f"{task} t: v1 -> v2"]


def test_retired_dependency_keeps_its_dependent_waiting(store: EntryStore) -> None:
    """Silent failure caught: retiring a task silently unblocking work that needed it done."""
    first = new(store, "task", "a")
    waiting = new(store, "task", "b", waits_on=[first])
    run(store, retire, id=first, reason="wrong plan")
    assert [item.id for item in run(store, ready).ready] == []
    assert run(store, search, query="", kinds=["task"]).total == 1
    assert run(store, show, id=waiting).entries[0].state == f"waiting on {first}"


def test_set_projects_moves_an_entry_between_projects(store: EntryStore) -> None:
    store.write_project("alpha", host="h", paths=[], remove_paths=[])
    store.write_project("beta", host="h", paths=[], remove_paths=[])
    note = new(store, "note", "n", projects=["alpha"])
    result = run(store, set_projects, id=note, add=["beta"], remove=["alpha"], reason="moved")
    assert [ref.name for ref in result.projects] == ["beta"]
    assert [ref.name for ref in store.get_entry(note).projects] == ["beta"]


@pytest.mark.parametrize("kind", ["note", "claim", "preference", "skill"])
def test_a_non_work_write_draws_no_graph(store: EntryStore, kind: str) -> None:
    assert run(store, write, kind=kind, title="x").graph == []


def test_a_script_artifact_in_a_temporary_directory_is_warned_about(
    store: EntryStore, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: an entry pointing at a script the operating system will delete,
    which trails holds only by reference."""
    from trails import locations

    script = tmp_path / "analysis.py"
    script.write_text("print(1)\n")
    monkeypatch.setattr(locations, "temporary_roots", lambda: (tmp_path.resolve(),))
    result = run(
        store, write, kind="claim", title="x", artifacts=[ArtifactRef(location=str(script))]
    )
    [warning] = result.warnings
    assert "is a script in the temporary directory" in warning
    assert (
        f'write(id="{result.id}", base_version=1, reason="...", repos=["<repository>"])' in warning
    )
