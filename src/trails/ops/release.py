"""The `release` operation.

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


async def release(
    id: Annotated[str, Field(pattern=TASK_ID, description="The claimed task.")],  # noqa: A002
    reason: Annotated[str, Field(description="Why you are giving it back.")],
) -> TaskResult:
    """Give back a claimed task without closing it; it returns to the ready queue if nothing blocks it.

    Use this when: you stop working on a task that is not finished, so another agent can take it.

    Use instead: close, when it is done, dropped or failed; replace, when other tasks take it over.

    Returns: the task's new state and the task graph around it. Releasing a task nobody holds changes nothing.
    """
    store = context.store()
    return task_result(store, "release", store.release(id, reason))
