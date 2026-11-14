"""The `unlink` operation.

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


async def unlink(
    source: Annotated[str, Field(description="The entry the link is from.")],
    relation: Annotated[LinkRelation, Field(description="The relation of the link to remove.")],
    target: Annotated[str, Field(description="The entry the link is to.")],
    reason: Annotated[
        str,
        Field(description="Required: why the link no longer holds; it changes what is ready."),
    ],
) -> LinkResult:
    """Remove a current link. The add and the removal both stay in history.

    Use this when: a dependency or parent link is wrong or no longer needed, or a cycle warning names a link to break.

    Use instead: close(outcome="superseded", by=[...]), which moves dependents onto replacements itself; retire, when the entry itself is no longer true.

    Returns: the removal, and for part_of and waits_on the task graph around both ends with each entry's state. Removing a link that is not current changes nothing.
    """
    store = context.store()
    outcome = store.remove_entry_link(source, relation, target, reason)
    warnings = list(outcome.warnings)
    graph = []
    if relation in {"part_of", "waits_on"}:
        graph, extra = redraw(store, [source, target])
        warnings += extra
    return LinkResult(
        source_id=source,
        relation=relation,
        target_id=target,
        action="remove",
        changed=outcome.changed,
        target_version=None,
        graph=graph,
        warnings=warnings,
    )
