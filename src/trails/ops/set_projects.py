"""The `set_projects` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import SetProjectsResult

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def set_projects(
    id: Annotated[str, Field(description="The entry whose grouping changes.")],  # noqa: A002
    add: Annotated[list[str], Field(description="Project ids or exact unique names to add.")] = [],  # noqa: B006 — contracted default; never mutated
    remove: Annotated[
        list[str], Field(description="Project ids or exact unique names to remove.")
    ] = [],  # noqa: B006 — contracted default; never mutated
    reason: Annotated[str, Field(description="Why this grouping changed.")] = "",
) -> SetProjectsResult:
    """Change which projects an entry belongs to without writing a version.

    Use this when: an entry was written unscoped, a project was registered later, or an entry should be shared with or removed from a project. A preference with no project is global.

    Use instead: write(projects=...), to set memberships when creating an entry.

    Returns: the effective add and remove changes, the requested no-ops, and the entry's complete current membership.
    """
    outcome = context.store().set_entry_projects(id, add, remove, reason)
    return SetProjectsResult(
        entry_id=outcome.entry_id,
        changed=outcome.changed,
        unchanged_add=outcome.unchanged_add,
        unchanged_remove=outcome.unchanged_remove,
        projects=outcome.projects,
    )
