"""The `close` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._common import embed_after_write
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._params import TaskId
from trails.ops._task_result import task_result
from trails.vocab import CloseOutcome

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def close(
    id: TaskId,  # noqa: A002
    outcome: Annotated[
        CloseOutcome,
        Field(
            description=(
                "completed: done; tasks waiting on it can become ready. dropped: not needed. "
                "failed: tried and could not be done. Tasks waiting on a dropped or failed task "
                "keep waiting."
            )
        ),
    ],
    summary: Annotated[str, Field(description="What was done, or why not.")],
) -> TaskResult:
    """Close a task with an outcome and a summary.

    Use this when: a task is finished, abandoned or failed, including a parent task whose children are done. The store never closes a task itself.

    Use instead: replace, when other tasks take this one over; release, to stop working on a task that is not finished; create_task, to create follow-up tasks.

    Returns: the new state, the dependents with their new state, and the task graph with each task's children and waits_on targets as stored facts. The outcome is shown as your word, with your harness and session. A task that is already closed is refused with its outcome, who closed it and when.
    """
    store = context.store()
    closed = store.close(id, outcome, summary)
    _embedded, warnings = embed_after_write(store, [closed.entry.id])
    closed = closed.model_copy(update={"warnings": [*closed.warnings, *warnings]})
    return task_result(store, "close", closed)
