"""The `close` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.models import FollowUp
from trails.ops import context
from trails.ops._common import embed_after_write
from trails.ops._entry_types import TaskResult
from trails.ops._marks import Operation
from trails.ops._task_result import task_result
from trails.vocab import Outcome

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=True)


async def close(
    id: Annotated[str, Field(description="The goal or task to close.")],  # noqa: A002
    outcome: Annotated[
        Outcome,
        Field(
            description=(
                "completed: done; dependents become ready. dropped: not needed; dependents keep "
                "waiting and are flagged for replanning. failed: tried and could not be done; "
                "dependents keep waiting, and a parent needs a decision. superseded: replaced by "
                "the entries in by, onto which every dependent's waits_on link moves."
            )
        ),
    ],
    summary: Annotated[str, Field(description="What was done, or why not.")],
    by: Annotated[
        list[str],
        Field(description='With outcome="superseded": the goals or tasks that replace this one.'),
    ] = [],  # noqa: B006 — contracted default; never mutated
    follow_ups: Annotated[
        list[FollowUp],
        Field(
            description=(
                'New tasks found during the work, each {"title": ..., "body": "", "priority": '
                'null, "tags": []}; each is linked discovered_from this one and part_of its '
                "parent."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
) -> TaskResult:
    """Close a goal or task with an outcome and a summary, and create any follow-up tasks in the same call.

    Use this when: a task is finished, abandoned, failed or replaced, or a goal's success criteria are met or given up. The store never closes a goal itself.

    Use instead: release, to stop working on a task that is not finished; retire, when an entry is no longer true.

    Returns: the new state, the follow-up ids, the links a supersede moved, the dependents with their new state, the task graph, and a warning when a parent is closed with children not closed. Closing again with the same outcome changes nothing.
    """
    store = context.store()
    closed = store.close(id, outcome, summary, by, follow_ups)
    _embedded, warnings = embed_after_write(store, [closed.entry.id, *closed.follow_ups])
    closed = closed.model_copy(update={"warnings": [*closed.warnings, *warnings]})
    return task_result(store, "close", closed)
