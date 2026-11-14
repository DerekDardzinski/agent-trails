"""The `unlink_work` operation.

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


async def unlink_work(
    source: TaskId,
    relation: Annotated[WorkRelation, Field(description="waits_on or part_of.")],
    target: TaskId,
    reason: Annotated[
        str, Field(description="Why the link no longer holds; it changes what is ready.")
    ],
) -> LinkResult:
    """Remove a dependency or parent link between two tasks. The add and the removal both stay in history.

    Use this when: a dependency or parent link is wrong or no longer needed.

    Use instead: replace, which moves dependents onto the tasks that take over a task.

    Returns: the removal and the task graph around both ends. Removing a link that is not current changes nothing.
    """
    outcome = context.store().remove_entry_link(source, relation, target, reason)
    return link_result(context.store(), outcome, work=True)
