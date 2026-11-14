"""The `set_paths` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import socket
from typing import Annotated

from pydantic import Field

from trails.models import ProjectPath, ProjectRef
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import UpdateProjectResult
from trails.ops._project_write import after_project_write, normalized_paths
from trails.reachability import path_state

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def set_paths(
    project: Annotated[str, Field(description="Stable project id or exact unique name.")],
    paths: Annotated[
        list[ProjectPath],
        Field(
            description=(
                'Paths or URIs to add or replace, each {"path": ..., "label": ..., '
                '"access_instructions": ...}. Paths not named stay as they are.'
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    remove_paths: Annotated[
        list[str], Field(description="Current paths to remove, exactly as prime lists them.")
    ] = [],  # noqa: B006 — contracted default; never mutated
) -> UpdateProjectResult:
    """Add, replace or remove the places a registered project's files live. trails creates nothing at any path.

    Use this when: a project gains or loses a repository, data directory or URI, or a path's label or access instructions change.

    Use instead: revise_framing, to change what the project is for; register_project, to create a project.

    Returns: whether anything changed, the current paths with whether each exists, and warnings for a path that does not exist, a URI that is not checked, or a possible secret. Paths equal to the stored ones write nothing.
    """
    host = socket.gethostname()
    given = normalized_paths(paths, host)
    store = context.store()
    resolved = store.resolve_project(project)
    outcome = store.write_project(resolved.name, host, given, remove_paths, intent="update")
    warnings = after_project_write(store, outcome, given, None)
    current = outcome.project.current_framing
    return UpdateProjectResult(
        project=ProjectRef(id=outcome.project.id, name=outcome.project.name),
        changed=outcome.changed,
        paths=[(item, path_state(item)) for item in outcome.project.paths],
        framing=current,
        framing_changed=False,
        current_framing_version=current.version if current is not None else None,
        notes=[],
        warnings=warnings,
    )
