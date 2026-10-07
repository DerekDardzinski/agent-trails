"""Marks a binder reads and pydantic ignores.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FromFile:
    """Marks a parameter as also readable from a file on the CLI surface.

    The CLI binder synthesizes `--<name>-from-file` for any parameter carrying this mark,
    reads the file, and passes the resolved text to the ordinary parameter. The MCP binder
    ignores it, because JSON has no quoting problem.

    Placed in the `Annotated` metadata beside the pydantic `Field`; pydantic ignores
    metadata it does not recognize, and a test asserts the mark does not reach the MCP
    schema.
    """


@dataclass(frozen=True)
class Operation:
    """Where an operation is served and how a host should treat it.

    APPLIED AS A MODULE ATTRIBUTE, `OPERATION = Operation(...)`, in the operation's own module,
    beside the function. Not as `Annotated` metadata on the return type: `ops.operations()`
    requires the return annotation to be a bare BaseModel subclass, and wrapping it in
    `Annotated` would make every operation fail that check.

    toolset: "agent" is the default MCP toolset; "full" is the optional MCP toolset; "cli" is
    never served over MCP. read_only, destructive and idempotent become the MCP annotations
    readOnlyHint, destructiveHint and idempotentHint.
    """

    toolset: Literal["agent", "full", "cli"]
    read_only: bool
    destructive: bool
    idempotent: bool
