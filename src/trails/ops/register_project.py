"""The `register_project` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import socket
import unicodedata
from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import engine as engine_seam, vector
from trails.errors import ValidationError
from trails.models import Project, ProjectFramingDraft, ProjectPath, ProjectRef
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._project_types import RegisterProjectResult
from trails.reachability import is_uri, path_state
from trails.secret_scan import scan_for_secrets

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def register_project(
    name: Annotated[
        str,
        Field(
            description=(
                "Unique one-line human name for a stable body of work. A repeated exact name "
                "returns the same generated project id."
            )
        ),
    ],
    paths: Annotated[
        list[ProjectPath],
        Field(
            description=(
                'Initial places the project uses, each {"path": ..., "label": ..., '
                '"access_instructions": ...}: repositories, data directories, or URIs. '
                "Use update_project for later changes."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    framing: Annotated[
        ProjectFramingDraft | None,
        Field(
            description=(
                "Optional project purpose and motivation: a one-line title plus why the "
                "project exists, who it serves, long-term success, and what is out of scope. "
                "Put operational knowledge in paths or ordinary records instead."
            )
        ),
    ] = None,
) -> RegisterProjectResult:
    """Register stable project identity, paths, and optional purpose framing in one transaction.

    Use this when: creating a stable body of work. Repeating the same registration returns the
    existing project without changing it. For an existing project, different framing or a
    nonempty path list that does not exactly match all current paths refuses with an
    update_project call.

    Use instead: update_project, to change paths or framing after registration; publish, to
    record knowledge in zero or more projects; set_projects, to change an existing record's
    memberships without revising its content.

    Returns: the stable project id and name, all current paths, the framing version written or
    recognized as a retry, the current framing version, deterministic near-name notes, and path,
    embedding, or possible-secret warnings.
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
    outcome = store.write_project(
        name,
        host,
        given,
        [],
        framing=framing,
        intent="register",
    )

    notes = _near_name_notes(outcome.project, store.list_projects()) if outcome.created else []
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
        except Exception as exc:  # framing is durable; report post-write embedding failure
            warnings.append(
                f"the framing was written but not embedded for similarity search: {exc}"
            )

    return RegisterProjectResult(
        project=ProjectRef(id=outcome.project.id, name=outcome.project.name),
        created=outcome.created,
        changed=outcome.changed,
        paths=[(item, path_state(item)) for item in outcome.project.paths],
        framing=outcome.framing,
        framing_created=outcome.framing_created,
        current_framing_version=outcome.current_framing_version,
        notes=notes,
        warnings=warnings,
    )


def _near_name_notes(created: Project, projects: list[Project]) -> list[str]:
    """At most three deterministic, nonblocking near-name notes."""
    source = _folded(created.name)
    compact = _compact(source)
    candidates: list[tuple[int, str, str, Project]] = []
    for project in projects:
        if project.id == created.id:
            continue
        other = _folded(project.name)
        distance = _levenshtein(source, other)
        threshold = max(1, min(3, round(max(len(source), len(other)) * 0.1)))
        if compact == _compact(other) or distance <= threshold:
            candidates.append(
                (0 if compact == _compact(other) else distance, other, project.id, project)
            )
    return [
        f"similar registered project: {project.id} · {project.name}"
        for _distance, _normal, _id, project in sorted(candidates)[:3]
    ]


def _folded(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _compact(value: str) -> str:
    return "".join(character for character in value if character.isalnum())


def _levenshtein(first: str, second: str) -> int:
    """Exact edit distance using one row of memory."""
    if len(first) < len(second):
        first, second = second, first
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, start=1):
        current = [row]
        for column, right in enumerate(second, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left != right),
                )
            )
        previous = current
    return previous[-1]
