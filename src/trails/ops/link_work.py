"""The `link_work` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import LinkResult
from trails.ops._links import link_result
from trails.ops._marks import Operation
from trails.ops._params import TaskId
from trails.vocab import WorkRelation

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def link_work(
    source: TaskId,
    relation: Annotated[
        WorkRelation,
        Field(
            description=(
                "waits_on: source is not ready until target is closed completed. part_of: "
                "source is a child of target."
            )
        ),
    ],
    target: TaskId,
    reason: Annotated[str | None, Field(description="Why the link holds; null for none.")] = None,
) -> LinkResult:
    """Add a dependency or parent link between two existing tasks, recording the target's current version.

    Use this when: an edge between tasks was found after they were created, such as a dependency on a task created later.

    Use instead: create_task(parent=..., waits_on=...), when creating a task whose edges you know; unlink_work, to remove a link; replace, when other tasks take over a task.

    Returns: the link, and the task graph around both ends with each task's state as stored facts. A link that already exists changes nothing. A link that would close a dependency cycle is refused, because no task on it could become ready; a target already closed is stored as given, and its state shows on the source.
    """
    outcome = context.store().add_entry_link(source, relation, target, reason)
    return link_result(context.store(), outcome, work=True)
