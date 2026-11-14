"""The `create_preference` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import Body, Title
from trails.ops._write import write

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def create_preference(
    title: Title,
    body: Body = "",
    projects: Annotated[
        list[str],
        Field(
            description=(
                "Project ids or exact names it applies to; [] makes it global, loaded in "
                "every session."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
) -> WriteResult:
    """Record how the person wants work done. prime prints preferences in full at session start. Every write records the harness, session and transcript that made it; you never pass them.

    Use this when: the person states how they want something done, or corrects how you did it.

    Use instead: create_note, for your own guess about what the person wants, until they confirm it; revise_preference, to change one.

    Returns: the id, warnings (a possible secret), and the show call.
    """
    draft = EntryDraft(kind="preference", title=title, body=body, projects=projects)
    return write(context.store(), "create_preference", draft, None)
