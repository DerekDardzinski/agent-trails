"""The `revise_task` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import BaseVersion, NewBody, NewTags, NewTitle, Reason
from trails.ops._write import write
from trails.vocab import TASK_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def revise_task(  # noqa: PLR0917 — the public revise contract
    id: Annotated[str, Field(pattern=TASK_ID, description="The task, tsk-...")],  # noqa: A002
    base_version: BaseVersion,
    reason: Reason,
    title: NewTitle = None,
    body: NewBody = None,
    tags: NewTags = None,
) -> WriteResult:
    """Write the next version of a task's title, body or tags. The id stays; the old version stays readable.

    Use this when: the plan for a task changes, or you add detail to it.

    Use instead: set_priority, to change its priority; link_work, to change what it waits on or is part of; close, claim, release or reopen, to change its state; set_projects, to change its projects.

    Returns: the id and new version, the task graph around it, warnings (a possible secret), and the show call. Content equal to the current version writes nothing and says so.
    """
    draft = EntryDraft(
        kind="task",
        id=id,
        base_version=base_version,
        reason=reason,
        title=title,
        body=body,
        tags=tags,
    )
    return write(context.store(), "revise_task", draft, None)
