"""The `prime` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails import retrieval
from trails.errors import StoreUnavailableError, UsageError
from trails.models import ProjectRef, Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import (
    PathStatus,
    PrimeContradicted,
    PrimeRecordRef,
    PrimeResult,
    PrimeTypeCount,
    ProjectIndexEntry,
)
from trails.reachability import path_state
from trails.vocab import ENTRY_TYPES

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
    last activity, the total project count, paging call, and current unscoped count. With
    project, a page whose size does not grow with history: current identity, paths and full
    framing; one title line per open problem statement with the show call that reads them;
    every current record that a current record contradicts, with those corrections; and per
    entry type the open and closed counts with the search calls that list each, plus disjoint
    current, replaced and retired counts. No other record is listed: search finds the rest.
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
                problem_statement_contradicted_by={},
                contradicted=[],
                counts=[],
                store_error=None,
            )

        registered = store.get_project(project)
        if registered is None:
            store.resolve_project(project)  # raises the stable public refusal
            raise AssertionError("unreachable")
        seen = retrieval.overview(store, registered.id)
        records = store.get_records(seen.current)
        incoming = retrieval.incoming_links_many(store, seen.current)
        contradicting_ids = [
            source
            for links in incoming.values()
            for relation, source in links
            if relation == "contradicts"
        ]
        sources = store.get_records(contradicting_ids)
    except StoreUnavailableError as exc:
        return _unavailable(str(exc))

    def corrections(record_id: str) -> list[PrimeRecordRef]:
        found = {
            source: _ref(sources[source])
            for relation, source in incoming.get(record_id, [])
            if relation == "contradicts"
            and source in sources
            and not sources[source].retired
            and sources[source].superseded_by is None
        }
        return list(found.values())

    tallies: dict[str, list[int]] = {}
    problem_statements: list[PrimeRecordRef] = []
    problem_contradicted: dict[str, list[PrimeRecordRef]] = {}
    contradicted: list[PrimeContradicted] = []
    for record_id in seen.current:
        record = records.get(record_id)
        if record is None:
            continue
        tally = tallies.setdefault(record.entry_type, [0, 0])
        tally[0 if record.work_status == "open" else 1] += 1
        against = corrections(record_id)
        if record.entry_type == "problem-statement" and record.work_status == "open":
            problem_statements.append(_ref(record))
            if against:
                problem_contradicted[record_id] = against
        elif against:
            contradicted.append(PrimeContradicted(record=_ref(record), contradicted_by=against))

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
        problem_statements=problem_statements,
        problem_statement_contradicted_by=problem_contradicted,
        contradicted=contradicted,
        counts=[
            PrimeTypeCount(entry_type=kind, open=tallies[kind][0], closed=tallies[kind][1])
            for kind in ENTRY_TYPES
            if kind in tallies
        ],
        store_error=None,
    )


def _ref(record: Record) -> PrimeRecordRef:
    return PrimeRecordRef(
        id=record.id,
        entry_type=record.entry_type,
        title=record.title,
        work_status=record.work_status,
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
        problem_statement_contradicted_by={},
        contradicted=[],
        counts=[],
        store_error=error,
    )
