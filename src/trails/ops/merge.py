"""The `merge` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import RetireResult
from trails.ops._marks import Operation
from trails.vocab import KNOWLEDGE_ID

OPERATION = Operation(toolset="dreamer", read_only=False, destructive=False, idempotent=True)


async def merge(
    id: Annotated[  # noqa: A002
        str, Field(pattern=KNOWLEDGE_ID, description="The duplicate note, preference or skill.")
    ],
    into: Annotated[
        str, Field(pattern=KNOWLEDGE_ID, description="The entry of the same kind it duplicates.")
    ],
    reason: Annotated[str, Field(description="One sentence saying why they are duplicates.")],
) -> RetireResult:
    """Retire a duplicate, recording which entry it duplicates. It stays in history.

    Use this when: the consolidation pass finds two notes, preferences or skills that say the same thing.

    Use instead: retire, when an entry is wrong and nothing replaces it; revise_note, revise_preference or revise_skill, to fold the duplicate's content into the one that stays first.

    Returns: confirmation, with the duplicate's retirement naming the entry it duplicates. Merging an entry already retired changes nothing.
    """
    outcome = context.store().retire_entry(id, reason, merged_into=into)
    return RetireResult(
        entry_id=outcome.entry_id,
        reason=outcome.reason,
        already_retired=outcome.already_retired,
        warnings=outcome.warnings,
    )
