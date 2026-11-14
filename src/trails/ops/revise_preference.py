"""The `revise_preference` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.models import EntryDraft
from trails.ops import context
from trails.ops._entry_types import WriteResult
from trails.ops._marks import Operation
from trails.ops._params import BaseVersion, NewBody, NewTitle, Reason
from trails.ops._write import write
from trails.vocab import PREFERENCE_ID

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def revise_preference(
    id: Annotated[  # noqa: A002
        str, Field(pattern=PREFERENCE_ID, description="The preference, prf-...")
    ],
    base_version: BaseVersion,
    reason: Reason,
    title: NewTitle = None,
    body: NewBody = None,
) -> WriteResult:
    """Write the next version of a preference. The id stays; the old version stays readable.

    Use this when: the person changes or refines how they want something done.

    Use instead: retire, when the preference no longer holds; set_projects, to change where it applies.

    Returns: the id and new version, warnings (a possible secret), and the show call. Content equal to the current version writes nothing and says so.
    """
    draft = EntryDraft(
        kind="preference",
        id=id,
        base_version=base_version,
        reason=reason,
        title=title,
        body=body,
    )
    return write(context.store(), "revise_preference", draft, None)
