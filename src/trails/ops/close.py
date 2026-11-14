"""The `close` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._common import embed_after_write
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._task_result import task_result
from trails.vocab import Outcome

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def close(
    id: Annotated[str, Field(description="The task to close.")],  # noqa: A002
    outcome: Annotated[
        Outcome,
        Field(
            description=(
                "completed: done; tasks waiting on it can become ready. dropped: not needed. "
                "failed: tried and could not be done. Tasks waiting on a dropped or failed task "
                "keep waiting. superseded: replaced by the tasks in by, onto which every "
                "dependent's waits_on link moves."
            )
        ),
    ],
    summary: Annotated[str, Field(description="What was done, or why not.")],
    by: Annotated[
        list[str],
        Field(description='With outcome="superseded": the tasks that replace this one.'),
    ] = [],  # noqa: B006 — contracted default; never mutated
) -> TaskResult:
    """Close a task with an outcome and a summary.

    Use this when: a task is finished, abandoned, failed or replaced, including a parent task whose children are done. The store never closes a task itself.

    Use instead: release, to stop working on a task that is not finished; create_task, to create follow-up tasks.

    Returns: the new state, the links a supersede moved, the dependents with their new state, and the task graph with each task's children and waits_on targets as stored facts. Closing again with the same outcome changes nothing.
    """
    store = context.store()
    closed = store.close(id, outcome, summary, by)
    _embedded, warnings = embed_after_write(store, [closed.entry.id])
    closed = closed.model_copy(update={"warnings": [*closed.warnings, *warnings]})
    return task_result(store, "close", closed)
