"""The `link` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._common import redraw
from trails.ops._entry_types import LinkResult
from trails.ops._marks import Operation
from trails.vocab import LinkRelation

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def link(
    source: Annotated[str, Field(description="The entry the link is from.")],
    relation: Annotated[
        LinkRelation,
        Field(
            description=(
                "waits_on: source is not ready until target is closed completed. part_of: "
                "source is a child of target. discovered_from: source came up while working on "
                "target. supersedes: source replaces target. supports or contradicts: source is "
                "evidence for or against target. references: source mentions target."
            )
        ),
    ],
    target: Annotated[str, Field(description="The entry the link is to.")],
    reason: Annotated[str | None, Field(description="Optional: why the link holds.")] = None,
) -> LinkResult:
    """Add a link between two existing entries, recording the target's current version.

    Use this when: an edge was not known at creation, such as a dependency on a task created later, or a note that supports a claim.

    Use instead: write(parent=..., waits_on=...), when creating an entry whose edges you know; unlink, to remove a link.

    Returns: the link and the target version it was written against, the task graph around both ends for part_of and waits_on with each entry's state, and warnings such as a dependency cycle. A link that already exists changes nothing.
    """
    store = context.store()
    outcome = store.add_entry_link(source, relation, target, reason)
    warnings = list(outcome.warnings)
    graph = []
    if relation in {"part_of", "waits_on"}:
        graph, extra = redraw(store, [source, target])
        warnings += extra
    return LinkResult(
        source_id=source,
        relation=relation,
        target_id=target,
        action="add",
        changed=outcome.changed,
        target_version=outcome.target_version,
        graph=graph,
        warnings=warnings,
    )
