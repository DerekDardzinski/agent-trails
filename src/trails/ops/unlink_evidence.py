"""The `unlink_evidence` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import LinkResult
from trails.ops._links import link_result
from trails.ops._marks import Operation
from trails.vocab import KNOWLEDGE_ID, EvidenceRelation

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def unlink_evidence(
    source: Annotated[
        str, Field(pattern=KNOWLEDGE_ID, description="The note, preference or skill linking.")
    ],
    relation: Annotated[EvidenceRelation, Field(description="supports or contradicts.")],
    target: Annotated[
        str, Field(pattern=KNOWLEDGE_ID, description="The note, preference or skill linked to.")
    ],
    reason: Annotated[str, Field(description="Why the link no longer holds.")],
) -> LinkResult:
    """Remove a supports or contradicts link. The add and the removal both stay in history.

    Use this when: the evidence turned out not to bear on the target.

    Use instead: retire, when the source itself is no longer true.

    Returns: the removal. Removing a link that is not current changes nothing.
    """
    outcome = context.store().remove_entry_link(source, relation, target, reason)
    return link_result(context.store(), outcome, work=False)
