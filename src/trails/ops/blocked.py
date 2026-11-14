"""The `blocked` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.errors import UsageError
from trails.ops import context
from trails.ops._entry_types import BlockedItem, BlockedResult
from trails.ops._marks import Operation
from trails.present import node_state, target_state

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def blocked(
    project: Annotated[
        str | None, Field(description="A project id or exact name; omit for every project.")
    ] = None,
    limit: Annotated[int, Field(description="Most blocked tasks to list.")] = 20,
) -> BlockedResult:
    """List the tasks that wait on a task not closed completed, with the stored state of each task they wait on.

    Use this when: you want to see what is held up and by what, for example a task waiting on one that was closed dropped or failed, which will not become ready until its links change.

    Use instead: ready, for the tasks you can start; show, for one task's full state.

    Returns: blocked tasks in the ready queue's order (priority, then age), each with every task it or an ancestor waits on that is not closed completed, and that task's state as stored (open, claimed, closed dropped, closed failed, closed superseded).
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    store = context.store()
    ref = store.resolve_project(project) if project is not None else None
    graph = store.task_graph()
    blocked_ids = graph.blocked(ref.id if ref is not None else None)
    items = [
        BlockedItem(
            id=item,
            version=graph.nodes[item].version,
            title=graph.nodes[item].title,
            priority=graph.nodes[item].priority,
            state=node_state(graph, item),
            waits_on=[
                f"{target}{f' through {via}' if via else ''} ({target_state(graph, target)})"
                for target, via in graph.waiting_on(item)
            ],
        )
        for item in blocked_ids[:limit]
    ]
    return BlockedResult(project=ref, blocked=items, total=len(blocked_ids))
