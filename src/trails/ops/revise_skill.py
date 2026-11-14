"""The `revise_skill` operation.

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
from trails.vocab import SKILL_ID

OPERATION = Operation(toolset="dreamer", read_only=False, destructive=False, idempotent=False)


async def revise_skill(  # noqa: PLR0917 — the public revise contract
    id: Annotated[str, Field(pattern=SKILL_ID, description="The skill, skl-...")],  # noqa: A002
    base_version: BaseVersion,
    reason: Reason,
    title: NewTitle = None,
    body: NewBody = None,
    tags: NewTags = None,
) -> WriteResult:
    """Write the next version of a skill. The id stays; the old version stays readable.

    Use this when: the consolidation pass finds a skill incomplete or out of date.

    Use instead: retire, when the skill no longer holds; merge, when another skill says the same thing.

    Returns: the id and new version, warnings (a possible secret), and the show call. Content equal to the current version writes nothing and says so.
    """
    draft = EntryDraft(
        kind="skill",
        id=id,
        base_version=base_version,
        reason=reason,
        title=title,
        body=body,
        tags=tags,
    )
    return write(context.store(), "revise_skill", draft, None)
