"""The `link_evidence` operation.

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


async def link_evidence(
    source: Annotated[
        str, Field(pattern=KNOWLEDGE_ID, description="The note, preference or skill linking.")
    ],
    relation: Annotated[
        EvidenceRelation,
        Field(description="supports or contradicts: source is evidence for or against target."),
    ],
    target: Annotated[
        str, Field(pattern=KNOWLEDGE_ID, description="The note, preference or skill linked to.")
    ],
    reason: Annotated[str | None, Field(description="Why the link holds; null for none.")] = None,
) -> LinkResult:
    """Record that one note, preference or skill supports or contradicts another, against the target's current version.

    Use this when: a finding backs or undercuts another one, or you have read a target that changed since you linked it and the link still holds.

    Use instead: unlink_evidence, when the link no longer holds; revise_note, to correct the content itself.

    Returns: the link and the target version it names. Linking again after the target has a new version records the link at that version, shown with your harness and session as your word that it still holds; at the current version already, nothing changes.
    """
    outcome = context.store().add_entry_link(source, relation, target, reason)
    return link_result(context.store(), outcome, work=False)
