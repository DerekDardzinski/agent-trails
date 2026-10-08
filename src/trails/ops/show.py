"""The `show` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import capture, retrieval
from trails.errors import RecordNotFoundError, ValidationError
from trails.models import Project, ProjectMembershipEvent, ProjectRef, Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import (
    ArtifactStatus,
    CaptureStatus,
    PathStatus,
    ProjectPathStatus,
    RelatedRecord,
    ShownProject,
    ShownRecord,
    ShowResult,
)
from trails.reachability import code_reachability, path_state, reachability
from trails.store import Store
from trails.vocab import PROJECT_ID_PATTERN, Detail, Relation

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def show(
    item_id: Annotated[
        str | list[str],
        Field(
            description=(
                "One exact record or project id, or an array mixing both. An array returns "
                "found items in first-occurrence order and names missing ids."
            )
        ),
    ],
    detail: Annotated[
        Detail,
        Field(description="summary, full or provenance for every requested item."),
    ] = "summary",
) -> ShowResult:
    """Read exact records or projects, including mixed arrays.

    Use this when: search or prime supplied an exact id.

    Use instead: search, to discover records or projects by meaning; prime, to orient to a
    project; lineage, to walk what one record builds on.

    Returns: record summary/full/provenance as before, with current memberships and full
    membership history; project summary, complete framing history, or path provenance. Arrays
    preserve first-occurrence request order in text and separate records and projects in data.
    """
    multiple = isinstance(item_id, list)
    requested = list(dict.fromkeys(item_id if multiple else [item_id]))
    if not requested:
        raise ValidationError("show needs at least one record or project id")

    store = context.store()
    found = store.get_records(requested)
    all_projects = {project.id: project for project in store.list_projects()}
    selected_projects = {
        item: all_projects[item]
        for item in requested
        if PROJECT_ID_PATTERN.fullmatch(item) is not None and item in all_projects
    }
    missing = [item for item in requested if item not in found and item not in selected_projects]
    if not multiple and missing:
        raise RecordNotFoundError(f"no record or project has id {missing[0]!r}")

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
    histories = store.project_membership_history(list(found)) if detail == "full" else {}
    store_dir = capture.store_path(store.db_path)
    records = [
        _shown_record(
            found[item],
            detail=detail,
            incoming=incoming.get(item, []),
            linked=linked,
            projects=all_projects,
            membership_history=histories.get(item, []),
            capture_store=store_dir,
        )
        for item in requested
        if item in found
    ]
    projects = [
        _shown_project(store, selected_projects[item], detail)
        for item in requested
        if item in selected_projects
    ]
    return ShowResult(
        requested=requested,
        records=records,
        projects=projects,
        missing=missing,
        detail=detail,
        multiple=multiple,
    )


def _shown_project(store: Store, project: Project, detail: Detail) -> ShownProject:
    count = retrieval.overview(store, project.id).current_count
    return ShownProject(
        project=ProjectRef(id=project.id, name=project.name),
        created_at=project.created_at,
        created_by=project.created_by,
        created_on_host=project.created_on_host,
        paths=[PathStatus(path=item, state=path_state(item)) for item in project.paths],
        current_framing=project.current_framing,
        framing_history=(store.project_framing_history(project.id) if detail == "full" else []),
        current_record_count=count,
    )


def _shown_record(
    record: Record,
    *,
    detail: Detail,
    incoming: list[tuple[Relation, str]],
    linked: dict[str, Record],
    projects: dict[str, Project],
    membership_history: list[ProjectMembershipEvent],
    capture_store: Path,
) -> ShownRecord:
    outgoing_related: list[RelatedRecord] = []
    incoming_related: list[RelatedRecord] = []
    if detail == "summary":
        outgoing_related = [
            RelatedRecord(
                relation=link.relation,
                direction="out",
                depth=1,
                record=linked[link.target_id],
            )
            for link in record.links
            if link.target_id in linked
        ]
        incoming_related = [
            RelatedRecord(relation=relation, direction="in", depth=1, record=linked[source])
            for relation, source in incoming
            if source in linked
        ]
    project_paths = [
        ProjectPathStatus(
            project=ProjectRef(id=project.id, name=project.name),
            path=path,
            state=path_state(path),
        )
        for ref in record.projects
        if (project := projects.get(ref.id)) is not None
        for path in project.paths
    ]
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
        project_paths=project_paths if detail == "provenance" else [],
        membership_history=membership_history,
        contradicted_by=sum(1 for relation, _source in incoming if relation == "contradicts"),
    )
