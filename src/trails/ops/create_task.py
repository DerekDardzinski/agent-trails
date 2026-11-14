"""The `create_task` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import Body, Priority, Projects, Tags, Title
from trails.ops._write import write
from trails.vocab import TASK_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def create_task(  # noqa: PLR0917 — the public create contract
    title: Title,
    body: Body = "",
    tags: Tags = [],  # noqa: B006 — contracted default; never mutated
    priority: Priority = None,
    parent: Annotated[
        str | None,
        Field(pattern=TASK_ID, description="The task this one is part of; null for none."),
    ] = None,
    waits_on: Annotated[
        list[Annotated[str, Field(pattern=TASK_ID)]],
        Field(description="Tasks that must be closed completed before this one is ready."),
    ] = [],  # noqa: B006 — contracted default; never mutated
    projects: Projects = [],  # noqa: B006 — contracted default; never mutated
) -> WriteResult:
    """Create one task, with its parent and the tasks it waits on, in one write. Every write records the harness, session and transcript that made it; you never pass them.

    Use this when: you plan work. Create a task after its parent and after the tasks it waits on, so their ids exist. A task with children is a parent; tag it goal if it is one.

    Use instead: link_work, for an edge found after creation; revise_task, to change a task's title, body or tags; set_priority, to change its priority.

    Returns: the id, the task graph around it with each task's state as stored facts, warnings (a possible secret, a temporary path in the body), and the show call. A parent or waits_on that would make a dependency cycle is refused.
    """
    draft = EntryDraft(
        kind="task",
        title=title,
        body=body,
        tags=tags,
        priority=priority,
        parent=parent,
        waits_on=waits_on,
        projects=projects,
    )
    return write(context.store(), "create_task", draft, None)
