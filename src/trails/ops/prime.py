"""The `prime` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field

from trails import retrieval
from trails.errors import StoreUnavailableError
from trails.models import Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import PathStatus, PrimeEntry, PrimeRelation, PrimeResult
from trails.reachability import path_state
from trails.vocab import Relation

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def prime(
    project: Annotated[
        str | None, Field(description="Project name. Omit to summarize the whole store.")
    ] = None,
) -> PrimeResult:
    """Start here at the beginning of a session or after a handoff. Shows the current state: registered paths, project context, problem statements, and every current record grouped by entry type. Replaced and retired records are counted but not listed. Results show what they build on and whether a verification record builds on them. Hypotheses show results built on this exact version, plus results that only cite or contradict them. Procedures show their latest verification stamp.

    Use this when: you are starting work on a project or do not know what the store holds.

    Use instead: search, to find something specific; show, to read one or several records in full; lineage, to inspect what a current record replaced.

    Returns: text beginning with disjoint current, replaced and retired counts. Project-context and problem-statement bodies are inline; other records are one line with their scientific connections. Complete show, lineage and search calls end the response.
    """
    try:
        store = context.store()
        registered_project = store.get_project(project) if project is not None else None
        registered = project is None or registered_project is not None
        paths = (
            [PathStatus(path=item, state=path_state(item)) for item in registered_project.paths]
            if registered_project is not None
            else []
        )
        seen = retrieval.overview(store, project)
        records = store.get_records(
            [*seen.current, *seen.project_contexts, *seen.problem_statements]
        )
        incoming = retrieval.incoming_links_many(store, seen.current)
        incoming_ids = [source for links in incoming.values() for _relation, source in links]
        target_ids = [
            link.target_id
            for record_id in seen.current
            if (record := records.get(record_id)) is not None
            for link in record.links
            if link.relation == "builds_on"
        ]
        related = {**records, **store.get_records([*incoming_ids, *target_ids])}
    except StoreUnavailableError as exc:
        return _unavailable(project, str(exc))

    context_ids = set(seen.project_contexts)
    problem_ids = set(seen.problem_statements)
    groups: dict[str, list[PrimeEntry]] = {}
    for record_id in seen.current:
        record = records.get(record_id)
        if record is None or record_id in context_ids or record_id in problem_ids:
            continue
        entry = _entry(record, incoming.get(record_id, []), related)
        groups.setdefault(record.entry_type, []).append(entry)

    return PrimeResult(
        project=project,
        registered=registered,
        paths=paths,
        current_count=seen.current_count,
        replaced_count=seen.replaced_count,
        retired_count=seen.retired_count,
        project_contexts=[records[i] for i in seen.project_contexts if i in records],
        problem_statements=[records[i] for i in seen.problem_statements if i in records],
        groups=groups,
        store_error=None,
    )


def _entry(
    record: Record,
    incoming: Sequence[tuple[Relation, str]],
    related: dict[str, Record],
) -> PrimeEntry:
    """One current record with the current incoming records and its builds_on targets."""
    sources = [
        (relation, related[source])
        for relation, source in incoming
        if source in related
        and not related[source].retired
        and related[source].superseded_by is None
    ]
    return PrimeEntry(
        record=record,
        builds_on=[
            related[link.target_id]
            for link in record.links
            if link.relation == "builds_on" and link.target_id in related
        ],
        incoming_results=[
            PrimeRelation(relation=relation, record=source)
            for relation, source in sources
            if source.entry_type == "result" and relation in {"builds_on", "cites", "contradicts"}
        ],
        verifications=[
            source
            for relation, source in sources
            if source.entry_type == "verification" and relation == "builds_on"
        ],
    )


def _unavailable(project: str | None, error: str) -> PrimeResult:
    return PrimeResult(
        project=project,
        registered=False,
        paths=[],
        current_count=0,
        replaced_count=0,
        retired_count=0,
        project_contexts=[],
        problem_statements=[],
        groups={},
        store_error=error,
    )
