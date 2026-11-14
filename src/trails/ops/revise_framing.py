"""The `revise_framing` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import socket
from typing import Annotated

from pydantic import Field

from trails.models import ProjectFramingDraft, ProjectRef
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import UpdateProjectResult
from trails.ops._project_write import after_project_write
from trails.reachability import path_state

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def revise_framing(
    project: Annotated[str, Field(description="Stable project id or exact unique name.")],
    framing: Annotated[
        ProjectFramingDraft,
        Field(
            description=(
                "The complete new purpose and motivation: a one-line title, and why the project "
                "exists, who it serves, long-term success and what is out of scope."
            )
        ),
    ],
    base_version: Annotated[
        int,
        Field(
            description=(
                "The framing version you read, from prime or show; 0 when the project has no "
                "framing yet. A stale one is refused."
            )
        ),
    ],
    reason: Annotated[str, Field(description="One sentence saying why the framing changed.")],
) -> UpdateProjectResult:
    """Write the next version of a project's framing. Earlier versions stay readable.

    Use this when: what the project is for, who it serves, or what counts as success changes.

    Use instead: set_paths, to change where its files live; create_note, for operational knowledge.

    Returns: whether a framing version was written, the current framing and version, and warnings for a possible secret or a failed embedding. A framing equal to the current one writes nothing.
    """
    host = socket.gethostname()
    store = context.store()
    resolved = store.resolve_project(project)
    outcome = store.write_project(
        resolved.name,
        host,
        [],
        [],
        framing=framing,
        base_version=base_version,
        reason=reason,
        intent="update",
    )
    warnings = after_project_write(store, outcome, [], framing)
    current = outcome.project.current_framing
    return UpdateProjectResult(
        project=ProjectRef(id=outcome.project.id, name=outcome.project.name),
        changed=outcome.changed,
        paths=[(item, path_state(item)) for item in outcome.project.paths],
        framing=outcome.framing or current,
        framing_changed=outcome.framing_created,
        current_framing_version=current.version if current is not None else None,
        notes=[],
        warnings=warnings,
    )
