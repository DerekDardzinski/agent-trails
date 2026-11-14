"""The `comment` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._comments import comment_result
from trails.ops._entry_types import CommentResult
from trails.ops._marks import FromFile, Operation
from trails.vocab import ENTRY_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def comment(
    id: Annotated[str, Field(pattern=ENTRY_ID, description="The entry the comment is on.")],  # noqa: A002
    comment: Annotated[str, Field(description="The feedback, in Markdown."), FromFile()],
) -> CommentResult:
    """Leave feedback on an entry without writing a version. It stays open until a reply answers it.

    Use this when: you question or correct a plan, a note or a change and expect an answer. A comment on a task records the version of every task under it, so changes_since_comment can list exactly what moved after it.

    Use instead: reply, to answer open comments; review, for a verdict on a task; revise_task or revise_note, for your own working thoughts.

    Returns: the comment id, the entry version it was written against, how many versions it pinned, and warnings (a possible secret).
    """
    return comment_result(context.store().add_comment(id, comment))
