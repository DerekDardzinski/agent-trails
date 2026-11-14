"""The `replace` operation.

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
from trails.vocab import TASK_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def replace(
    id: TaskId,  # noqa: A002
    by: Annotated[
        list[Annotated[str, Field(pattern=TASK_ID)]],
        Field(min_length=1, description="The tasks that take over this one."),
    ],
    reason: Annotated[str, Field(description="Why these tasks replace it.")],
) -> TaskResult:
    """Close a task as superseded by other tasks, moving every task that waits on it onto each replacement.

    Use this when: a task is split into smaller tasks, or another plan takes it over.

    Use instead: close, when a task is done, dropped or failed with no replacement.

    Returns: the new state, the waits_on links that moved, the dependents with their new state, and the task graph. Each replacement records a supersedes link to the replaced task. Moves that would close a dependency cycle are refused. A task that is already closed is refused with its outcome, who closed it and when.
    """
    store = context.store()
    replaced = store.close(id, "superseded", reason, by)
    _embedded, warnings = embed_after_write(store, [replaced.entry.id])
    replaced = replaced.model_copy(update={"warnings": [*replaced.warnings, *warnings]})
    return task_result(store, "replace", replaced)
