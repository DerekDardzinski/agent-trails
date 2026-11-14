"""The `reply` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._comments import comment_result
from trails.ops._entry_types import CommentResult
from trails.ops._marks import FromFile, Operation
from trails.vocab import COMMENT_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def reply(
    addresses: Annotated[
        list[Annotated[str, Field(pattern=COMMENT_ID)]],
        Field(
            min_length=1,
            description="The open comments this answers, cmt-..., all on one entry; they close.",
        ),
    ],
    comment: Annotated[str, Field(description="The answer, in Markdown."), FromFile()],
) -> CommentResult:
    """Answer open comments on one entry; the reply closes them.

    Use this when: you have acted on feedback or decided not to, and say which.

    Use instead: comment, for new feedback that needs its own answer.

    Returns: the reply's id, the comments it closed, and warnings (a possible secret). Comments on different entries, a reply, or a comment already answered cannot be addressed and are refused.
    """
    store = context.store()
    on = store.get_comment(addresses[0]).entry_id
    return comment_result(store.add_comment(on, comment, addresses))
