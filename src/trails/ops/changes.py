"""The `changes` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field

from trails.errors import UsageError
from trails.ops import context
from trails.ops._maint_types import ChangesResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="dreamer", read_only=True, destructive=False, idempotent=True)


async def changes(
    since: Annotated[datetime, Field(description="Start of the window, ISO 8601, inclusive.")],
    until: Annotated[
        datetime | None,
        Field(description="End of the window, ISO 8601, exclusive; null for now."),
    ] = None,
    session: Annotated[
        str | None, Field(description="A session id whose writes to list; null for every session.")
    ] = None,
    limit: Annotated[int, Field(description="Most changes to list, oldest first.")] = 200,
    offset: Annotated[int, Field(description="Changes to skip, for the next page.")] = 0,
) -> ChangesResult:
    """List every write in a time window, optionally by one session: versions, link changes, task events and comments, each with its author, harness and session.

    Use this when: the consolidation pass reviews what sessions wrote since it last ran.

    Use instead: changes_since_comment, for what moved under one commented entry; show, for one entry's history.

    Returns: the changes oldest first up to limit, the total in the window, and the call for the next page.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    if offset < 0:
        raise UsageError(f"offset must be 0 or more, not {offset}")
    found = context.store().changes(since, until, session)
    return ChangesResult(
        since=since,
        until=until,
        session=session,
        changes=found[offset : offset + limit],
        total=len(found),
        offset=offset,
    )
