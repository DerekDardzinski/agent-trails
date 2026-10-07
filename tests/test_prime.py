"""Tests for the agent's current-state `prime` view against real Dolt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from trails.models import Link, RecordDraft
from trails.ops import context
from trails.ops.prime import prime
from trails.ops.register_project import register_project
from trails.store import Store
from trails.vocab import PROJECT_CONTEXT_TEMPLATE


def _call(store: Store, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _write(store: Store, title: str, body: str = "", **fields: Any) -> str:
    fields.setdefault("entry_type", "note")
    fields.setdefault("work_status", "closed")
    return store.write_record(RecordDraft(title=title, body=body, **fields)).record.id


def test_registered_project_context_is_labeled_and_template_body_is_not_printed(
    store: Store,
) -> None:
    """Catches the project context using the old name or positional fields, or six empty
    template headings consuming the orientation response."""
    registered = _call(store, register_project, name="mint")

    result = _call(store, prime, project="mint")
    text = result.render_text()

    assert registered.project_context_created is True
    assert result.project_contexts[0].id == registered.project_context_id
    assert result.project_contexts[0].title == "mint"
    assert result.project_contexts[0].entry_type == "project-context"
    assert result.project_contexts[0].work_status == "open"
    assert result.project_contexts[0].tags == []
    assert text.splitlines()[:2] == [
        "state: project mint · 1 current records · 0 replaced by newer versions · 0 retired",
        "project paths: none",
    ]
    assert "project context:\n  project: mint" in text
    assert f"  record id: {registered.project_context_id}" in text
    assert f'  read full record: show("{registered.project_context_id}")' in text
    assert "  template only, nothing recorded" in text
    assert "## Goal" not in text
    assert "brief" not in text.lower()


def test_prime_lists_only_current_records_grouped_by_kind_and_counts_the_rest(store: Store) -> None:
    """Catches a replaced or retired record presented as current, a body printed for an
    ordinary record, or groups not ordered newest first."""
    store.write_project("mint", "host", [], [])
    old_context = _write(
        store, "old", PROJECT_CONTEXT_TEMPLATE, entry_type="project-context", project="mint"
    )
    current_context = _write(
        store,
        "mint",
        "## Goal\nMeasure ingest latency.",
        entry_type="project-context",
        project="mint",
        supersedes=old_context,
        reason="filled in the context",
    )
    older = _write(store, "Use 10-item batches", "old body", entry_type="decision", project="mint")
    newer = _write(store, "Use 25-item batches", "new body", entry_type="decision", project="mint")
    retired = _write(store, "Wrong fact", "retired body", entry_type="fact", project="mint")
    store.retire(retired, "wrong")

    result = _call(store, prime, project="mint")
    text = result.render_text()

    assert (result.current_count, result.replaced_count, result.retired_count) == (3, 1, 1)
    assert [entry.record.id for entry in result.groups["decision"]] == [newer, older]
    assert result.project_contexts[0].id == current_context
    assert "## Goal\nMeasure ingest latency." in text
    assert "old body" not in text and "new body" not in text and "retired body" not in text
    assert old_context not in text.split("decisions", 1)[-1]
    assert f"  {newer}  Use 25-item batches" in text
    assert text.index(newer) < text.index(older)


def test_prime_explains_result_hypothesis_and_verification_connections(store: Store) -> None:
    """Catches cites being treated as evidence, a result's replaced target hidden, or a
    verification and replacement relation missing from the current-state view."""
    old_hypothesis = _write(store, "Hypothesis: old", entry_type="hypothesis", project="mint")
    hypothesis = _write(
        store,
        "Hypothesis: revised",
        entry_type="hypothesis",
        project="mint",
        supersedes=old_hypothesis,
        reason="changed threshold",
    )
    measured_old = _write(
        store,
        "Result: old threshold",
        entry_type="result",
        project="mint",
        links=[Link(relation="builds_on", target_id=old_hypothesis)],
    )
    cited = _write(
        store,
        "Result: related observation",
        entry_type="result",
        project="mint",
        links=[Link(relation="cites", target_id=hypothesis)],
    )
    verification = _write(
        store,
        "Verification: rerun",
        entry_type="verification",
        project="mint",
        links=[Link(relation="builds_on", target_id=measured_old)],
    )

    text = _call(store, prime, project="mint").render_text()

    assert (
        f"{hypothesis}  Hypothesis: revised · work closed · no result built on this version" in text
    )
    assert f"replaces {old_hypothesis}" in text
    assert f"cited by: {cited} (result; cites does not count as a test, use builds_on)" in text
    assert (
        f"{measured_old}  Result: old threshold · work closed · verified by {verification}" in text
    )
    assert f"builds on {old_hypothesis} (hypothesis, work closed, replaced by {hypothesis})" in text
    assert (
        f"{cited}  Result: related observation · work closed · not verified · builds on nothing"
        in text
    )
    assert f'  inspect replacement history: lineage("{hypothesis}")' in text


def test_prime_includes_current_problem_statements_that_cite_the_project_context(
    store: Store,
) -> None:
    """Catches a cross-project problem statement omitted, or its body reduced to an index
    line when framing must be read before work starts."""
    store.write_project("mint", "host", [], [])
    project_context = _write(
        store, "mint", "## Goal\nMeasure lift", entry_type="project-context", project="mint"
    )
    local = _write(
        store,
        "Reduce variance",
        "## Objective\nCut variance.",
        entry_type="problem-statement",
        project="mint",
    )
    shared = _write(
        store,
        "Shared retrieval change",
        "## Objective\nImprove both projects.",
        entry_type="problem-statement",
        project="everest",
        links=[Link(relation="cites", target_id=project_context)],
    )
    old = _write(store, "Old framing", "old body", entry_type="problem-statement", project="mint")
    new = _write(
        store,
        "New framing",
        "new body",
        entry_type="problem-statement",
        project="mint",
        supersedes=old,
        reason="narrowed",
    )

    result = _call(store, prime, project="mint")
    text = result.render_text()

    assert [record.id for record in result.problem_statements] == [new, shared, local]
    assert "    ## Objective\n    Improve both projects." in text
    assert "old body" not in text


def test_prime_for_an_unregistered_project_says_so_and_keeps_its_current_records(
    store: Store,
) -> None:
    """Catches a misspelled project reading as empty when records already use that name."""
    fact = _write(store, "Known fact", entry_type="fact", project="ghost")

    result = _call(store, prime, project="ghost")
    lines = result.render_text().splitlines()

    assert result.registered is False
    assert lines[1] == (
        'project ghost is not registered: register_project(name="ghost", paths=[...])'
    )
    assert f"  {fact}  Known fact · work closed" in lines


def test_prime_on_an_unopenable_store_reports_it_as_data(tmp_path: Path) -> None:
    """Catches startup raising instead of reporting the store error and a next call."""
    not_a_directory = tmp_path / "db"
    not_a_directory.write_text("")

    async def go() -> Any:
        return await context.bind(prime, lambda: not_a_directory)(project=None)

    lines = asyncio.run(go()).render_text().splitlines()

    assert lines[0].startswith(
        f"state: store unavailable: cannot create a database at {not_a_directory}"
    )
    assert lines[1:] == ["Next: version()"]


def test_whole_store_prime_labels_each_problem_statements_project(store: Store) -> None:
    """Catches two projects' same-titled framing becoming indistinguishable in the whole-store
    orientation view."""
    alpha = _write(
        store,
        "Same objective",
        "alpha body",
        entry_type="problem-statement",
        project="alpha",
    )
    beta = _write(
        store,
        "Same objective",
        "beta body",
        entry_type="problem-statement",
        project="beta",
    )

    text = _call(store, prime).render_text()

    assert f"project: beta\n  record id: {beta}\n  title: Same objective" in text
    assert f"project: alpha\n  record id: {alpha}\n  title: Same objective" in text


def test_prime_displays_work_status_for_every_current_record(store: Store) -> None:
    """Catches an agent needing a second query to distinguish open from closed current work."""
    open_id = _write(
        store, "Needs another run", entry_type="hypothesis", work_status="open", project="mint"
    )
    closed_id = _write(
        store, "Recorded result", entry_type="result", work_status="closed", project="mint"
    )

    text = _call(store, prime, project="mint").render_text()

    assert f"{open_id}  Needs another run · work open" in text
    assert f"{closed_id}  Recorded result · work closed" in text
