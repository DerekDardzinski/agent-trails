"""The `retire` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails import dolt
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._write_types import RetireResult

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def retire(
    record_id: Annotated[str, Field(description="The record to retire.")],
    reason: Annotated[str, Field(description="One sentence saying why it is no longer true.")],
) -> RetireResult:
    """Mark a record as no longer true. It stays in history, and search hides it unless include_history is true.

    Use this when: a fact, procedure or decision is wrong or obsolete and nothing replaces it.

    Use instead: publish(supersedes=..., reason=...) when there is a corrected version.

    Returns: confirmation. Retiring a retired record changes nothing.
    """
    store = context.store()
    # Under one exclusive lock, so two concurrent retires cannot both read "not retired" and
    # both report already_retired=False.
    with dolt.exclusive(store.db_path):
        before = store.query("SELECT reason FROM retirement WHERE record_id = ?", [record_id])
        store.retire(record_id, reason)
    if before:
        # The store kept the first reason and wrote nothing; report the reason that stands.
        return RetireResult(
            record_id=record_id, reason=str(before[0]["reason"]), already_retired=True
        )
    return RetireResult(record_id=record_id, reason=reason.strip(), already_retired=False)
