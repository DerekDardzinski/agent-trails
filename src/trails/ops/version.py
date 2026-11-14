"""The `version` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails import __version__
from trails.ops import context
from trails.ops._maint_types import VersionResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="full", read_only=True, destructive=False, idempotent=True)


async def version() -> VersionResult:
    """Report the trails version and database path.

    Use this when: you need to confirm which store you are talking to.

    Use instead: prime, for what the store holds.

    Returns: the package version and the database path.
    """
    return VersionResult(version=__version__, db_path=str(context.db_path()))
