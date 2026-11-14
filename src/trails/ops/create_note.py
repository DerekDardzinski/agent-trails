"""The `create_note` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import Artifacts, Body, Projects, Repos, Tags, Title, Value
from trails.ops._write import write

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def create_note(  # noqa: PLR0917 — the public create contract
    title: Title,
    body: Body = "",
    tags: Tags = [],  # noqa: B006 — contracted default; never mutated
    value: Value = None,
    artifacts: Artifacts = [],  # noqa: B006 — contracted default; never mutated
    repos: Repos = [],  # noqa: B006 — contracted default; never mutated
    projects: Projects = [],  # noqa: B006 — contracted default; never mutated
) -> WriteResult:
    """Record something known or found: a result, a fact, a finding, a guess about what the person wants. Every write records the harness, session and transcript that made it; you never pass them.

    Use this when: you learn something a later session would otherwise rediscover. Pass the data in artifacts and the code that produced it in repos.

    Use instead: create_preference, for how the person wants work done; create_task, for something to do; revise_note, to change a note; link_evidence, to say one note supports or contradicts another.

    Returns: the id, one line per captured repository, warnings (a possible secret, data or code in a temporary folder or outside git, a path that does not exist), and the show call.
    """
    draft = EntryDraft(
        kind="note",
        title=title,
        body=body,
        tags=tags,
        value=value,
        artifacts=artifacts,
        projects=projects,
    )
    return write(context.store(), "create_note", draft, repos)
