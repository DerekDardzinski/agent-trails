"""The `changes_since_comment` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import ChangesSinceCommentResult
from trails.ops._marks import Operation
from trails.vocab import COMMENT_ID

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def changes_since_comment(
    comment: Annotated[str, Field(pattern=COMMENT_ID, description="A comment id, cmt-...")],
) -> ChangesSinceCommentResult:
    """List what changed under the commented entry after a comment: new versions and link changes.

    Use this when: you return to work you commented on or reviewed, to see what moved since.

    Use instead: show, for the entry's full current state.

    Returns: each entry under the commented one whose version moved (vN -> vM, or new since the comment), and each link added or removed after it, with its reason.
    """
    since = context.store().changes_since_comment(comment)
    return ChangesSinceCommentResult(
        comment_id=since.comment.id,
        entry_id=since.comment.entry_id,
        entry_version=since.comment.entry_version,
        changed=[
            f"{change.entry_id} {change.title}: "
            + (
                f"v{change.pinned} -> v{change.current}"
                if change.pinned is not None
                else f"new since the comment (v{change.current})"
            )
            for change in since.changed
        ],
        links=[
            f"{event.action} link {event.source_id} {event.relation} {event.target_id}"
            + (f" · {event.reason}" if event.reason else "")
            for event in since.links
        ],
    )
