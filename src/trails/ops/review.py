"""The `review` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._comments import comment_result
from trails.ops._entry_types import CommentResult
from trails.ops._marks import FromFile, Operation
from trails.ops._params import TaskId
from trails.vocab import Verdict

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def review(
    id: TaskId,  # noqa: A002
    verdict: Annotated[Verdict, Field(description="approve or changes_requested.")],
    comment: Annotated[
        str, Field(description="What you checked and found, in Markdown; may be empty."), FromFile()
    ] = "",
) -> CommentResult:
    """Record a review verdict on a task, pinned to the version of the task and of every task under it.

    Use this when: you reviewed work you did not write and give it a verdict.

    Use instead: comment, for feedback without a verdict; reply, to answer a review.

    Returns: the comment id, the versions it pinned, and warnings (a possible secret). The verdict is shown as your word, with your harness and session; trails does not check it.
    """
    return comment_result(context.store().add_comment(id, comment, verdict=verdict))
