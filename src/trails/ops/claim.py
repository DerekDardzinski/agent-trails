"""The `claim` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._task_result import task_result

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def claim(
    id: Annotated[str, Field(description="The task to claim.")],  # noqa: A002
    base_version: Annotated[
        int, Field(description="The task version you read, from show or ready.")
    ],
    take_over_reason: Annotated[
        str | None,
        Field(
            description=(
                "Required only to take a task another session holds, including your own "
                "earlier session after a handoff: why taking it over is right."
            )
        ),
    ] = None,
) -> TaskResult:
    """Claim a task for this session, so other agents see it is taken.

    Use this when: you are about to work on a ready task.

    Use instead: release, to give a task back without closing it; close, when the work is done or abandoned.

    Returns: the task's new state and the task graph around it. Claiming a task this session already holds changes nothing.
    """
    store = context.store()
    return task_result(store, "claim", store.claim(id, base_version, take_over_reason))
