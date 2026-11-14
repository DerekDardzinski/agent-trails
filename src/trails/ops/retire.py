"""The `retire` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import RetireResult
from trails.ops._marks import Operation
from trails.vocab import KNOWLEDGE_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def retire(
    id: Annotated[  # noqa: A002
        str, Field(pattern=KNOWLEDGE_ID, description="The note, preference or skill to retire.")
    ],
    reason: Annotated[str, Field(description="One sentence saying why it is no longer true.")],
) -> RetireResult:
    """Mark a note, preference or skill as no longer true. It stays in history, and search hides it unless include_retired is true.

    Use this when: a note, preference or skill is wrong or obsolete and nothing replaces it.

    Use instead: revise_note or revise_preference, to correct it; close or replace, to end a task.

    Returns: confirmation. Retiring a retired entry keeps the first reason and changes nothing.
    """
    outcome = context.store().retire_entry(id, reason)
    return RetireResult(
        entry_id=outcome.entry_id,
        reason=outcome.reason,
        already_retired=outcome.already_retired,
        warnings=outcome.warnings,
    )
