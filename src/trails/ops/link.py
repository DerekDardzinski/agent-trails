"""The `link` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails import dolt
from trails.errors import ValidationError
from trails.ops import context
from trails.ops._guidance import result_hypothesis_link_notes
from trails.ops._marks import Operation
from trails.ops._write_types import LinkResult
from trails.vocab import Relation

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def link(
    source_id: Annotated[str, Field(description="The record the relation is from.")],
    relation: Annotated[Relation, Field(description="builds_on, contradicts or cites.")],
    target_id: Annotated[str, Field(description="The record the relation is to.")],
) -> LinkResult:
    """Relate two existing records with builds_on, contradicts or cites.

    Use this when: you notice a relation after both records exist.

    Use instead: publish(links=...) when writing the new record; publish(supersedes=..., reason=...) to replace a record, which link refuses.

    Returns: the link and any note explaining that cites or contradicts does not count as a result built on a hypothesis. Adding a link that already exists changes nothing.
    """
    # `relation` admits all four values so that supersedes reaches this refusal, which names
    # the call to make, instead of a schema rejection that names nothing.
    if relation == "supersedes":
        raise ValidationError(
            "link does not write supersedes: a replacement needs a new record and a reason; "
            "use publish(supersedes=..., reason=...)"
        )
    store = context.store()
    with dolt.exclusive(store.db_path):
        records = store.get_records([source_id, target_id])
        stored = store.add_link(source_id, relation, target_id)
    source = records.get(source_id)
    target = records.get(target_id)
    notes = (
        result_hypothesis_link_notes(
            source_id,
            source.entry_type,
            [*source.links, stored],
            {target_id: target},
        )
        if source is not None and target is not None
        else []
    )
    return LinkResult(
        source_id=source_id,
        relation=stored.relation,
        target_id=stored.target_id,
        notes=notes,
    )
