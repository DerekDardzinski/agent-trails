"""The `create_skill` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import Body, Projects, Tags, Title
from trails.ops._write import write

OPERATION = Operation(toolset="dreamer", read_only=False, destructive=False, idempotent=False)


async def create_skill(
    title: Title,
    body: Body = "",
    tags: Tags = [],  # noqa: B006 — contracted default; never mutated
    projects: Projects = [],  # noqa: B006 — contracted default; never mutated
) -> WriteResult:
    """Record how to do something, distilled from earlier sessions. Every write records the harness, session and transcript that made it; you never pass them.

    Use this when: the consolidation pass finds a procedure that worked across sessions.

    Use instead: create_note, for one session's finding; revise_skill, to change a skill; merge, when two skills say the same thing.

    Returns: the id, warnings (a possible secret, a temporary path in the body), and the show call.
    """
    draft = EntryDraft(kind="skill", title=title, body=body, tags=tags, projects=projects)
    return write(context.store(), "create_skill", draft, None)
