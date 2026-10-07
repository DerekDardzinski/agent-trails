"""The `set_work_status` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._write_types import SetWorkStatusResult
from trails.vocab import WorkStatus

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def set_work_status(
    record_id: Annotated[str, Field(description="The record whose expected work changes.")],
    work_status: Annotated[
        WorkStatus,
        Field(
            description="Whether another action is expected on this exact record: open or closed."
        ),
    ],
    reason: Annotated[
        str,
        Field(
            description=(
                "Why the expectation changed. Required for an actual transition; ignored when "
                "the record already has the requested status."
            )
        ),
    ],
) -> SetWorkStatusResult:
    """Change whether more work is expected on one record, while keeping every earlier status event.

    Use this when: the expectation of more work changes but the record's content does not.

    Use instead: publish(supersedes=..., reason=...) when the record's content changes; retire when the record is no longer true or applicable.

    Returns: the latest work status, whether a new event was written, and the show call for the record. Any entry type may move in either direction. Trails records the caller's decision and imposes no workflow conditions.
    """
    event, changed = context.store().set_work_status(record_id, work_status, reason)
    return SetWorkStatusResult(
        record_id=record_id,
        work_status=event.work_status,
        reason=event.reason,
        changed=changed,
    )
