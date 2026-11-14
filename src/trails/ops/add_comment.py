"""The `add_comment` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import CommentResult
from trails.ops._marks import FromFile, Operation
from trails.vocab import Verdict

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def add_comment(
    id: Annotated[str, Field(description="The entry the comment is on.")],  # noqa: A002
    comment: Annotated[str, Field(description="The feedback or the response."), FromFile()],
    addresses: Annotated[
        list[str],
        Field(
            description=(
                "Open comment ids on this entry that this comment responds to; they close. "
                "Omit for new feedback, which stays open until a comment addresses it."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    verdict: Annotated[
        Verdict | None,
        Field(description="A review verdict, approve or changes_requested, pinned to versions."),
    ] = None,
) -> CommentResult:
    """Comment on an entry without writing a version: review feedback, or the response that closes it.

    Use this when: you review a plan, a claim or a change and need a response, or you answer feedback. A comment on a goal or task records the version of every task under it, so the next review can list exactly what changed.

    Use instead: write(id=..., reason=...), for your own working thoughts, which belong in a version's reason or body; never a comment.

    Returns: the comment id, the entry version it was written against, the comments it closed, and how many versions it pinned.
    """
    outcome = context.store().add_comment(id, comment, addresses, verdict)
    return CommentResult(
        comment_id=outcome.comment.id,
        entry_id=outcome.comment.entry_id,
        entry_version=outcome.comment.entry_version,
        verdict=outcome.comment.verdict,
        addresses=outcome.comment.addresses,
        pinned=len(outcome.pinned),
        warnings=outcome.warnings,
    )
