"""The `set_projects` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.errors import ValidationError
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import SetProjectsResult

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def set_projects(
    record_id: Annotated[str, Field(description="Existing record id whose grouping changes.")],
    add: Annotated[
        list[str],
        Field(description="Project ids or exact unique names to add to the record."),
    ] = [],  # noqa: B006 — contracted default; never mutated
    remove: Annotated[
        list[str],
        Field(description="Project ids or exact unique names to remove from the record."),
    ] = [],  # noqa: B006 — contracted default; never mutated
    reason: Annotated[
        str,
        Field(description="Why this grouping changed. Required when add or remove is given."),
    ] = "",
) -> SetProjectsResult:
    """Append effective project-membership changes without changing the record itself.

    Use this when: a record was published unscoped, a project was created later, or an existing
    record should be shared with or removed from a project.

    Use instead: publish with projects, to set initial membership atomically with a new record;
    publish with supersedes, to revise record content.

    Returns: effective add/remove changes, requested no-ops, and the record's complete current
    project membership.
    """
    store = context.store()
    requested = [*add, *remove]
    resolved = store.resolve_project_values(requested) if requested else []
    add_refs = resolved[: len(add)]
    remove_refs = resolved[len(add) :]
    refs = list({ref.id: ref for ref in resolved}.values())
    add_ids = list(dict.fromkeys(ref.id for ref in add_refs))
    remove_ids = list(dict.fromkeys(ref.id for ref in remove_refs))
    overlap = sorted(set(add_ids) & set(remove_ids))
    if overlap:
        names = ", ".join(
            next(ref.name for ref in refs if ref.id == project_id) for project_id in overlap
        )
        raise ValidationError(f"{names} is present in both add and remove")
    outcome = store.set_projects(record_id, add_ids, remove_ids, reason)
    return SetProjectsResult(**outcome.model_dump())
