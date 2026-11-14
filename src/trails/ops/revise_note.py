"""The `revise_note` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated, Any

from pydantic import Field

from trails.models import ArtifactRef, EntryDraft, RepoCapture
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import BaseVersion, NewBody, NewTags, NewTitle, Reason
from trails.ops._write import write
from trails.vocab import NOTE_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def revise_note(  # noqa: PLR0917 — the public revise contract
    id: Annotated[str, Field(pattern=NOTE_ID, description="The note, note-...")],  # noqa: A002
    base_version: BaseVersion,
    reason: Reason,
    title: NewTitle = None,
    body: NewBody = None,
    tags: NewTags = None,
    value: Annotated[
        dict[str, Any] | None,
        Field(description="The new JSON object; omit to keep it, {} to clear it."),
    ] = None,
    artifacts: Annotated[
        list[ArtifactRef] | None,
        Field(
            description=(
                'The new data references, each {"location": ..., "digest": null or '
                '"sha256:<64 lowercase hex>", "notes": ...}; omit to keep them, [] to clear '
                "them."
            )
        ),
    ] = None,
    repos: Annotated[
        list[RepoCapture] | None,
        Field(
            description=(
                'Repositories to capture now, each {"path": ..., "exclude": [...], '
                '"allow_large": [...]}; omit to keep the previous captures, [] to clear them.'
            )
        ),
    ] = None,
) -> WriteResult:
    """Write the next version of a note. The id stays; the old version stays readable.

    Use this when: a note turns out wrong or incomplete, or its data or code moved.

    Use instead: retire, when nothing replaces it; link_evidence, to relate it to another note; set_projects, to change its projects.

    Returns: the id and new version, one line per captured repository, warnings (a possible secret, data or code in a temporary folder or outside git), and the show call. Content equal to the current version writes nothing and says so.
    """
    draft = EntryDraft(
        kind="note",
        id=id,
        base_version=base_version,
        reason=reason,
        title=title,
        body=body,
        tags=tags,
        value=value,
        artifacts=artifacts,
    )
    return write(context.store(), "revise_note", draft, repos)
