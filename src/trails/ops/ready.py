"""The `ready` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.errors import UsageError
from trails.ops import context
from trails.ops._entry_types import ReadyItem, ReadyResult
from trails.ops._marks import Operation
from trails.present import graph_lines, node_state

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def ready(
    project: Annotated[
        str | None, Field(description="A project id or exact name; omit for every project.")
    ] = None,
    limit: Annotated[int, Field(description="Most ready tasks to list.")] = 20,
) -> ReadyResult:
    """List the tasks ready to work on, most urgent first, and the goals and tasks waiting on a decision.

    Use this when: you need the next thing to do, or to see what is blocked on replanning.

    Use instead: show, for one task's full state; search(status=...), to list tasks by state.

    Returns: ready tasks by priority then age, each with its claim if one is held; entries ready to close; parents with a failed child that need a decision; and entries whose dependency was dropped or failed and need replanning.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    store = context.store()
    ref = store.resolve_project(project) if project is not None else None
    graph = store.task_graph()
    scope = ref.id if ref is not None else None
    ready_ids = graph.ready(scope)

    def in_scope(entry_id: str) -> bool:
        return scope is None or scope in graph.nodes[entry_id].projects

    work = [item for item in graph.nodes if graph.is_work(item) and in_scope(item)]
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

    def lines(ids: list[str]) -> list:
        wanted = set(ids)
        return [line for line in graph_lines(graph, ids) if line.id in wanted]

    return ReadyResult(
        project=ref,
        ready=items,
        total=len(ready_ids),
        ready_to_close=lines([item for item in work if graph.is_ready_to_close(item)]),
        needs_decision=lines([item for item in work if graph.needs_decision(item)]),
        needs_replanning=lines(graph.needs_replanning(scope)),
    )
