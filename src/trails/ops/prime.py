"""The `prime` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field

from trails import retrieval
from trails.errors import StoreUnavailableError, UsageError
from trails.models import ProjectRef, Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import (
    PathStatus,
    PrimeEntry,
    PrimeRelation,
    PrimeResult,
    ProjectIndexEntry,
)
from trails.reachability import path_state
from trails.vocab import Relation

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def prime(
    project: Annotated[
        str | None,
        Field(
            description=(
                "Stable project id or exact unique name. Omit for the small whole-store project index."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum projects in a whole-store index page; ignored for scoped prime."
        ),
    ] = 100,
    offset: Annotated[
        int,
        Field(description="Projects to skip in the whole-store index; ignored for scoped prime."),
    ] = 0,
) -> PrimeResult:
    """Orient to the whole store or one project's current framing, paths, and records.

    Use this when: beginning a session, choosing among registered projects, or resuming work in
    one known project.

    Use instead: search, to discover records or projects by meaning; show, to read exact record
    or project history; lineage, to inspect what a record replaced or built on.

    Returns: without project, up to 100 project ids, names, framing titles, current/open counts,
    last activity, the total project count, paging call, and current unscoped count. With project, current identity, paths, full current
    framing, full current problem statements, and one line per other current record, plus disjoint
    current, replaced and retired counts.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    if offset < 0:
        raise UsageError(f"offset must be 0 or more, not {offset}")
    try:
        store = context.store()
        if project is None:
            indexed, unscoped = retrieval.project_index(store)
            page = indexed[offset : offset + limit]
            return PrimeResult(
                project=None,
                projects=[
                    ProjectIndexEntry(
                        project=ProjectRef(id=item.project_id, name=item.name),
                        framing_title=item.framing_title,
                        record_count=item.record_count,
                        open_count=item.open_count,
                        last_activity=item.last_activity,
                    )
                    for item in page
                ],
                project_total=len(indexed),
                offset=offset,
                limit=limit,
                unscoped_count=unscoped,
                paths=[],
                framing=None,
                current_count=0,
                replaced_count=0,
                retired_count=0,
                problem_statements=[],
                groups={},
                store_error=None,
            )

        registered = store.get_project(project)
        if registered is None:
            store.resolve_project(project)  # raises the stable public refusal
            raise AssertionError("unreachable")
        seen = retrieval.overview(store, registered.id)
        records = store.get_records([*seen.current, *seen.problem_statements])
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
        return _unavailable(str(exc))

    problem_ids = set(seen.problem_statements)
    groups: dict[str, list[PrimeEntry]] = {}
    for record_id in seen.current:
        record = records.get(record_id)
        if record is None or record_id in problem_ids:
            continue
        groups.setdefault(record.entry_type, []).append(
            _entry(record, incoming.get(record_id, []), related)
        )

    return PrimeResult(
        project=ProjectRef(id=registered.id, name=registered.name),
        projects=[],
        project_total=0,
        offset=0,
        limit=limit,
        unscoped_count=0,
        paths=[PathStatus(path=item, state=path_state(item)) for item in registered.paths],
        framing=registered.current_framing,
        current_count=seen.current_count,
        replaced_count=seen.replaced_count,
        retired_count=seen.retired_count,
        problem_statements=[records[item] for item in seen.problem_statements if item in records],
        groups=groups,
        store_error=None,
    )


def _entry(
    record: Record,
    incoming: Sequence[tuple[Relation, str]],
    related: dict[str, Record],
) -> PrimeEntry:
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
        contradicted_by=list(
            {
                source.id: source for relation, source in sources if relation == "contradicts"
            }.values()
        ),
    )


def _unavailable(error: str) -> PrimeResult:
    return PrimeResult(
        project=None,
        projects=[],
        project_total=0,
        offset=0,
        limit=100,
        unscoped_count=0,
        paths=[],
        framing=None,
        current_count=0,
        replaced_count=0,
        retired_count=0,
        problem_statements=[],
        groups={},
        store_error=error,
    )
