"""The `lineage` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails import retrieval
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import LineageResult, RelatedRecord

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def lineage(
    record_id: Annotated[str, Field(description="The record to start from.")],
) -> LineageResult:
    """Show what a record was built on and what was built on it, following builds_on and supersedes links up to 5 steps in each direction.

    Use this when: you need the history behind a result or the versions of a procedure.

    Use instead: search(links=...), for a single relation such as contradicts.

    Returns: one line per related record with the relation and its direction.
    """
    store = context.store()
    record = store.get_record(record_id)
    ancestors, descendants = retrieval.lineage_walk(store, record_id)
    records = store.get_records([other for _, other, _ in [*ancestors, *descendants]])
    return LineageResult(
        record=record,
        ancestors=[
            RelatedRecord(relation=relation, direction="out", depth=depth, record=records[other])
            for relation, other, depth in ancestors
            if other in records
        ],
        descendants=[
            RelatedRecord(relation=relation, direction="in", depth=depth, record=records[other])
            for relation, other, depth in descendants
            if other in records
        ],
    )
