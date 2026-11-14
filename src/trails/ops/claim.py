"""The `claim` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._params import TaskId
from trails.ops._task_result import task_result

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def claim(
    id: TaskId,  # noqa: A002
    base_version: Annotated[
        int, Field(description="The task version you read, from show or ready.")
    ],
    holder: Annotated[
        str | None,
        Field(
            description=(
                "Who held the task when you read it, as show and ready print it (a session id, "
                "or a harness name); null when it was unclaimed."
            )
        ),
    ] = None,
) -> TaskResult:
    """Claim a task for this session, so other agents see it is taken.

    Use this when: you are about to work on a ready task, or to take over a task another session holds, such as your own earlier session after a handoff.

    Use instead: release, to give a task back without closing it; close, when the work is done or abandoned.

    Returns: the task's new state and the task graph around it. A holder that is not the current one is refused like a stale base_version, with the call to retry. Claiming a task this session already holds changes nothing; a closed task is refused with its outcome, who closed it and when.
    """
    store = context.store()
    return task_result(store, "claim", store.claim(id, base_version, holder))
