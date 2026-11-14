"""The `reopen` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._task_result import task_result
from trails.vocab import TASK_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def reopen(
    id: Annotated[str, Field(pattern=TASK_ID, description="The closed task.")],  # noqa: A002
    reason: Annotated[str, Field(description="Why it needs more work.")],
) -> TaskResult:
    """Move a closed task back to open. Links that a supersede moved stay moved.

    Use this when: closed work turns out not to be finished, or you closed it with the wrong outcome and want to close it again.

    Use instead: create_task, to create a new task when the new work is separate.

    Returns: the new state, its dependents and the task graph around it. Reopening an entry that is not closed changes nothing.
    """
    store = context.store()
    return task_result(store, "reopen", store.reopen(id, reason))
