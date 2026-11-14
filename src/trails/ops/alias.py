"""The `alias` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.ops import context
from trails.ops._maint_types import AliasResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def alias(
    tag: Annotated[str, Field(description="The tag to treat as equivalent.")],
    canonical: Annotated[str, Field(description="The tag it is equivalent to.")],
) -> AliasResult:
    """Record that two tags mean the same thing, so a search for either finds both. Nothing is renamed.

    Use this when: lint lists two tags as likely duplicates, or you notice two spellings of one tag.

    Use instead: nothing; this is the only way to join tags.

    Returns: the tag and the canonical tag it now matches.
    """
    context.store().write_tag_alias(tag, canonical)
    # The store normalises both tags as entry tags are normalised; report what it stored.
    return AliasResult(tag=tag.strip().lower(), canonical=canonical.strip().lower())
