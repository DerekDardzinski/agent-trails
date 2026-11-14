"""The `set_priority` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import BaseVersion, Priority, Reason, TaskId
from trails.ops._write import write

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def set_priority(
    id: TaskId,  # noqa: A002
    base_version: BaseVersion,
    priority: Priority,
    reason: Reason,
) -> WriteResult:
    """Change a task's priority, as a new version of the task.

    Use this when: a task becomes more or less urgent.

    Use instead: revise_task, to change its title, body or tags.

    Returns: the id and new version, the task graph around it, and the show call. The same priority as the current version writes nothing and says so.
    """
    draft = EntryDraft(
        kind="task",
        id=id,
        base_version=base_version,
        reason=reason,
        priority=priority,
        priority_given=True,
    )
    return write(context.store(), "set_priority", draft, None)
