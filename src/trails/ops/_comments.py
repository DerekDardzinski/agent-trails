"""The response every comment-writing operation shares.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import CommentOutcome
from trails.ops._entry_types import CommentResult


def comment_result(outcome: CommentOutcome) -> CommentResult:
    return CommentResult(
        comment_id=outcome.comment.id,
        entry_id=outcome.comment.entry_id,
        entry_version=outcome.comment.entry_version,
        verdict=outcome.comment.verdict,
        addresses=outcome.comment.addresses,
        pinned=len(outcome.pinned),
        warnings=outcome.warnings,
    )
