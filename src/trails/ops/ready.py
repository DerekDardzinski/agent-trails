"""The `ready` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.errors import UsageError
from trails.ops import context
from trails.ops._entry_types import ReadyItem, ReadyResult
from trails.ops._marks import Operation
from trails.present import node_state

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def ready(
    project: Annotated[
        str | None, Field(description="A project id or exact name; omit for every project.")
    ] = None,
    limit: Annotated[int, Field(description="Most ready tasks to list.")] = 20,
) -> ReadyResult:
    """List the tasks ready to work on, most urgent first.

    Use this when: you need the next thing to do.

    Use instead: blocked, for the tasks waiting on other tasks and the state of each; show, for one task's full state; search(status=...), to list tasks by state.

    Returns: ready tasks by priority then age, each with its claim if one is held, and the call that claims the first. A task is ready when it is not closed, none of its children is open, and every task it or an ancestor waits on is closed completed.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    store = context.store()
    ref = store.resolve_project(project) if project is not None else None
    graph = store.task_graph()
    ready_ids = graph.ready(ref.id if ref is not None else None)
    items = []
    for item in ready_ids[:limit]:
        node = graph.nodes[item]
        held = node.claim
        items.append(
            ReadyItem(
                id=item,
                version=node.version,
                title=node.title,
                priority=node.priority,
                state=node_state(graph, item),
                claimed_by=(held.session_id or held.harness) if held is not None else None,
                claimed_at=held.created_at if held is not None else None,
            )
        )
    return ReadyResult(project=ref, ready=items, total=len(ready_ids))
