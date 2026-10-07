"""The `mark_verified` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._maint_types import MarkVerifiedResult
from trails.ops._marks import Operation

# idempotent: stamping the same record, date and environment again writes nothing.
OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def mark_verified(
    record_id: Annotated[str, Field(description="The record that was confirmed.")],
    environment: Annotated[
        str, Field(description="Where it was confirmed, for example green or red.")
    ],
) -> MarkVerifiedResult:
    """Record that a record, usually a procedure, was confirmed to work today in a named environment.

    Use this when: you just followed a procedure and it worked.

    Use instead: publish(supersedes=..., reason=...) when the procedure needed changes to work. The new procedure starts unverified; a stamp belongs only to the exact record that was run.

    Returns: confirmation with the date stamped.
    """
    # The local date: "today" for the person who followed the procedure, not UTC's.
    today = datetime.now().astimezone().date()
    context.store().stamp_verified(record_id, environment, today)
    return MarkVerifiedResult(record_id=record_id, verified_at=today, environment=environment)
