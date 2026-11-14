"""The `update_project` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import socket
from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import engine as engine_seam, vector
from trails.errors import ValidationError
from trails.models import ProjectFramingDraft, ProjectPath, ProjectRef
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import UpdateProjectResult
from trails.reachability import is_uri, path_state
from trails.secret_scan import scan_for_secrets

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def update_project(  # noqa: PLR0917 — one atomic public update
    project: Annotated[str, Field(description="Stable project id or exact unique name to update.")],
    paths: Annotated[
        list[ProjectPath],
        Field(
            description=(
                'Paths or URIs to add or replace, each {"path": ..., "label": ..., '
                '"access_instructions": ...}. Omitted paths remain unchanged.'
            )
        ),
    ] = [],  # noqa: B006
    remove_paths: Annotated[
        list[str], Field(description="Current paths to remove, exactly as prime lists them.")
    ] = [],  # noqa: B006
    framing: Annotated[
        ProjectFramingDraft | None,
        Field(
            description=(
                "Complete new purpose and motivation. Unchanged title/body is a no-op. "
                "Operational knowledge belongs in paths or ordinary entries."
            )
        ),
    ] = None,
    base_version: Annotated[
        int | None,
        Field(
            description=(
                "Current framing version shown by search, show, or prime. Required when framing "
                "changes; use 0 when the project has no framing. Path-only updates need none."
            )
        ),
    ] = None,
    reason: Annotated[
        str | None,
        Field(description="Why framing changed. Required for changed post-registration framing."),
    ] = None,
) -> UpdateProjectResult:
    """Apply effective path and framing changes to an existing project atomically.

    Use this when: a registered project's paths, purpose, motivation, success definition, or
    exclusions change.

    Use instead: register_project, to create stable identity; set_projects, to change which
    projects contain an entry; write, to store operational knowledge as a claim or a note.

    Returns: whether anything changed, current paths, current framing and whether a new framing
    version was created, plus warnings. Unchanged input writes no Dolt commit.
    """
    host = socket.gethostname()
    given: list[ProjectPath] = []
    for item in paths:
        location = item.path.strip()
        if not is_uri(location):
            try:
                location = str(Path(location).expanduser())
            except RuntimeError as exc:
                raise ValidationError(f"cannot expand {item.path}: {exc}") from exc
        given.append(
            item.model_copy(
                update={
                    "path": location,
                    "host": "" if is_uri(location) else (item.host or host),
                }
            )
        )

    store = context.store()
    resolved = store.resolve_project(project)
    outcome = store.write_project(
        resolved.name,
        host,
        given,
        remove_paths,
        framing=framing,
        base_version=base_version,
        reason=reason,
        intent="update",
    )

    warnings: list[str] = []
    for item in given:
        state = path_state(item)
        if is_uri(item.path):
            kind = "s3 URI" if item.path.startswith("s3://") else "URI"
            warnings.append(f"{item.path}: not checked ({kind})")
        elif state == "missing":
            warnings.append(f"{item.path} does not exist on {item.host}")
    fields = {
        f"{item.path} {part}": text
        for item in given
        for part, text in (
            ("label", item.label),
            ("access_instructions", item.access_instructions),
        )
    }
    if framing is not None:
        fields.update({"framing.title": framing.title, "framing.body": framing.body})
    warnings += [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}"
        for finding in scan_for_secrets(fields)
    ]
    if outcome.framing_created and outcome.framing is not None:
        try:
            engine = engine_seam.build_engine()
            if engine.is_available():
                vector.embed_project_framings(store, engine, [(outcome.project, outcome.framing)])
        except Exception as exc:
            warnings.append(
                f"the framing was written but not embedded for similarity search: {exc}"
            )

    current_framing = outcome.project.current_framing
    return UpdateProjectResult(
        project=ProjectRef(id=outcome.project.id, name=outcome.project.name),
        changed=outcome.changed,
        paths=[(item, path_state(item)) for item in outcome.project.paths],
        framing=outcome.framing or current_framing,
        framing_changed=outcome.framing_created,
        current_framing_version=(current_framing.version if current_framing is not None else None),
        notes=[],
        warnings=warnings,
    )
