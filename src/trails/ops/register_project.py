"""The `register_project` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import socket
from contextlib import suppress
from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import engine as engine_seam, vector
from trails.errors import ValidationError
from trails.models import ProjectPath, RecordDraft
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import RegisterProjectResult
from trails.reachability import is_uri, path_state
from trails.secret_scan import scan_for_secrets
from trails.store import Store
from trails.vocab import PROJECT_CONTEXT_TEMPLATE

OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def register_project(
    name: Annotated[
        str, Field(description="Short project name used in every record's project field.")
    ],
    paths: Annotated[
        list[ProjectPath],
        Field(
            description=(
                'Places the project uses, each {"path": ..., "label": ..., '
                '"access_instructions": ...}: code repositories, data directories, s3:// '
                "prefixes. A path already registered is updated."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    remove_paths: Annotated[
        list[str],
        Field(description="Registered paths to remove from the project, exactly as listed."),
    ] = [],  # noqa: B006 — contracted default; never mutated
) -> RegisterProjectResult:
    """Register a project, or add, update or remove its paths. Creates nothing on disk: a project is a name, the paths it uses with a label and how to reach each, and a project-context record with empty headings, created when the project has none.

    Use this when: work starts on something that will span sessions, prime reports the project is not registered, or the project starts using another repository, data directory or bucket.

    Use instead: publish, to add records to a project; export and publish(from_file=...), to fill in or revise the project context.

    Returns: the project's paths with whether each exists on this machine, the project context's record id, and warnings for a local path that does not exist or a possible secret in a label or access instructions.
    """
    name = name.strip()
    if not name:
        raise ValidationError("project name is empty")
    host = socket.gethostname()
    given: list[ProjectPath] = []
    for item in paths:
        location = item.path.strip()
        if not is_uri(location):
            try:
                location = str(Path(location).expanduser())
            except RuntimeError as exc:  # expanduser on an unknown user, as in "~nosuchuser/x"
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
    project, created = store.write_project(name, host, given, remove_paths)

    warnings: list[str] = []
    for item in given:
        state = path_state(item)
        if is_uri(item.path):
            kind = "s3 URI" if item.path.startswith("s3://") else "URI"
            warnings.append(f"{item.path}: not checked ({kind})")
        elif state == "missing":
            warnings.append(f"{item.path} does not exist on {item.host}")
    findings = scan_for_secrets(
        {
            f"{item.path} {part}": text
            for item in given
            for part, text in (
                ("label", item.label),
                ("access_instructions", item.access_instructions),
            )
        }
    )
    warnings += [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}"
        for finding in findings
    ]

    project_context_id = _current_project_context(store, name)
    project_context_created = project_context_id is None
    if project_context_id is None:
        project_context = RecordDraft(
            entry_type="project-context",
            work_status="open",
            title=name,
            body=PROJECT_CONTEXT_TEMPLATE,
            tags=[],
            project=name,
        )
        project_context_id = store.write_record(project_context).record.id
        _embed_quietly(store, project_context_id)

    return RegisterProjectResult(
        project=project.name,
        created=created,
        paths=[(item, path_state(item)) for item in project.paths],
        project_context_id=project_context_id,
        project_context_created=project_context_created,
        warnings=warnings,
    )


def _embed_quietly(store: Store, record_id: str) -> None:
    """Embed the new project context for similarity search, as publish embeds every record it writes.

    THE PROJECT CONTEXT IS DURABLE by now, so nothing here may raise, for the reason publish gives. A
    failure leaves the project context unembedded, which lint's unembedded category and search's state
    line both report, and `embed` repairs.
    """
    with suppress(Exception):
        engine = engine_seam.build_engine()
        if engine.is_available():
            vector.embed_record(store, engine, record_id)


def _current_project_context(store: Store, name: str) -> str | None:
    """The newest project context of the project that nothing supersedes, or None."""
    rows = store.query(
        "SELECT r.id FROM record r "
        "WHERE r.entry_type = 'project-context' AND r.project = ? AND NOT EXISTS ("
        "SELECT 1 FROM link l WHERE l.relation = 'supersedes' AND l.target_id = r.id) "
        "AND NOT EXISTS (SELECT 1 FROM retirement x WHERE x.record_id = r.id) "
        "ORDER BY r.created_at DESC LIMIT 1",
        [name],
    )
    return str(rows[0]["id"]) if rows else None
