"""The `show` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import capture, retrieval
from trails.errors import RecordNotFoundError, ValidationError
from trails.models import Project, Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import (
    ArtifactStatus,
    CaptureStatus,
    PathStatus,
    RelatedRecord,
    ShownRecord,
    ShowResult,
)
from trails.reachability import code_reachability, path_state, reachability
from trails.vocab import Detail, Relation

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def show(
    record_id: Annotated[
        str | list[str],
        Field(
            description=(
                "One record id, or an array. An array returns found records in first-occurrence "
                "order and names missing ids without failing the found records."
            )
        ),
    ],
    detail: Annotated[
        Detail, Field(description="summary, full or provenance for every record.")
    ] = "summary",
) -> ShowResult:
    """Read one or several records. A missing single id is an error. An array names missing ids and returns every found record in the requested order; duplicate ids appear once.

    Use this when: search or prime gave you one or more ids.

    Use instead: lineage, to walk what one record builds on; export, to edit one record.

    Returns: summary gives each entry type, title, descriptive tags, lifecycle, latest work status, the first 12 body lines, links in and out, and artifacts with whether each still exists. full gives each whole body and ordered work-status history. provenance adds captured code commits with the git command to read their files, artifacts with digests compared, registered project paths, and structured values. Array output has one final Next line and labels every missing id.
    """
    multiple = isinstance(record_id, list)
    requested = list(dict.fromkeys(record_id if multiple else [record_id]))
    if not requested:
        raise ValidationError("show needs at least one record id")

    store = context.store()
    found = store.get_records(requested)
    missing = [item for item in requested if item not in found]
    if not multiple and missing:
        raise RecordNotFoundError(f"no record has id {missing[0]!r}")

    incoming = retrieval.incoming_links_many(store, list(found))
    related_ids = [
        other
        for record in found.values()
        for other in [
            *(link.target_id for link in record.links),
            *(source for _relation, source in incoming.get(record.id, [])),
        ]
    ]
    linked = {**found, **store.get_records(related_ids)} if detail == "summary" else found
    projects = (
        {project.name: project for project in store.list_projects()}
        if detail == "provenance"
        else {}
    )
    store_dir = capture.store_path(store.db_path)
    records = [
        _shown_record(
            found[item],
            detail=detail,
            incoming=incoming.get(item, []),
            linked=linked,
            projects=projects,
            capture_store=store_dir,
        )
        for item in requested
        if item in found
    ]
    return ShowResult(
        requested=requested,
        records=records,
        missing=missing,
        detail=detail,
        multiple=multiple,
    )


def _shown_record(
    record: Record,
    *,
    detail: Detail,
    incoming: list[tuple[Relation, str]],
    linked: dict[str, Record],
    projects: dict[str, Project],
    capture_store: Path,
) -> ShownRecord:
    outgoing_related: list[RelatedRecord] = []
    incoming_related: list[RelatedRecord] = []
    if detail == "summary":
        outgoing_related = [
            RelatedRecord(
                relation=link.relation, direction="out", depth=1, record=linked[link.target_id]
            )
            for link in record.links
            if link.target_id in linked
        ]
        incoming_related = [
            RelatedRecord(relation=relation, direction="in", depth=1, record=linked[source])
            for relation, source in incoming
            if source in linked
        ]

    project = projects.get(record.project) if record.project is not None else None
    return ShownRecord(
        record=record,
        outgoing=outgoing_related,
        incoming=incoming_related,
        artifacts=[
            ArtifactStatus(ref=ref, reachability=reachability(ref, verify=detail == "provenance"))
            for ref in record.artifacts
        ]
        if detail != "full"
        else [],
        captures=[
            CaptureStatus(capture=item, reachability=code_reachability(item, capture_store))
            for item in record.code
        ]
        if detail == "provenance"
        else [],
        capture_store=str(capture_store),
        project_registered=project is not None,
        project_paths=[PathStatus(path=item, state=path_state(item)) for item in project.paths]
        if project is not None
        else [],
        contradicted_by=sum(1 for relation, _source in incoming if relation == "contradicts"),
    )
