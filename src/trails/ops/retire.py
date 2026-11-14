"""The `retire` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import RetireResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def retire(
    id: Annotated[str, Field(description="The entry to retire.")],  # noqa: A002
    reason: Annotated[str, Field(description="One sentence saying why it is no longer true.")],
) -> RetireResult:
    """Mark an entry as no longer true. It stays in history; search hides it unless include_retired is true, and it stops counting as a child or a dependency.

    Use this when: a claim, preference, note or skill is wrong or obsolete and nothing replaces it, or a task should vanish rather than be closed.

    Use instead: write(id=..., base_version=..., reason=...), to correct it; close, to finish a task with an outcome.

    Returns: confirmation. Retiring a retired entry keeps the first reason and changes nothing.
    """
    outcome = context.store().retire_entry(id, reason)
    return RetireResult(
        entry_id=outcome.entry_id,
        reason=outcome.reason,
        already_retired=outcome.already_retired,
        warnings=outcome.warnings,
    )
