"""The MCP surface, derived from the operations at import.

There is no per-operation branch here either, and no use of fastmcp's deprecated
`exclude_args`: an operation signature carries only operation parameters, so there is never a
parameter belonging to one surface only.

NOTE: no `from __future__ import annotations`. See `ops/__init__.py`.
"""

import os
import sys
from functools import wraps
from pathlib import Path
from typing import Any, Literal, get_type_hints

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

from trails import ops, sessions
from trails.errors import TrailsError
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops.types import RenderedResult

# The environment variable that selects the optional toolset when no argument does.
TOOLSET_ENV = "TRAILS_MCP_TOOLSET"
Toolset = Literal["agent", "full"]

# What a refusal looks like on this surface. It is an MCP error result, so clients cannot
# mistake a refused write for success, and it also carries structured readable content so an
# agent can act on the reason.
ERROR_SCHEMA = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}


def build_mcp_server(db_path: Path | None = None, toolset: Toolset | None = None) -> FastMCP:
    """The fastmcp server with the operations of one toolset registered as tools.

    Each operation is registered according to its `OPERATION` mark: toolset "agent" always;
    toolset "full" only when `toolset` is "full", or when `toolset` is None and
    `$TRAILS_MCP_TOOLSET` is "full"; toolset "cli" never. Each tool carries the MCP
    annotations readOnlyHint, destructiveHint and idempotentHint from the mark. Tools are named
    by `ops.tool_name`.

    AN UNMARKED OPERATION FAILS CONSTRUCTION, before anything is registered, so a new operation
    cannot silently land in the default toolset.

    Registers each operation through `ops.context.bind` with a `db_path` callable returning the
    argument, so the binding happens per awaited call and NOT at construction time: two servers
    constructed in one event-loop context would otherwise both observe the later binding.

    Returns the operation's `render_text()` as text content and its model as structured content,
    and derives the output schema from the model type. The model
    is ALWAYS a top-level BaseModel — `ops.operations()` refuses anything else — because a bare
    union or an optional model acquires a synthetic `{"result": ...}` envelope on this surface
    and not on the other.

    Translates a raised TrailsError into an MCP error result with readable structured content.
    A RuntimeError from an unbound store is a binder bug and is NOT translated.

    Raises:
        TypeError: a discovered operation's module has no `OPERATION = Operation(...)`.
        ValueError: `toolset` or `$TRAILS_MCP_TOOLSET` is something other than agent or full.
    """
    served = {"agent", "full"} if _resolved_toolset(toolset) == "full" else {"agent"}
    marked = [(operation, operation_mark(operation)) for operation in ops.operations()]
    server = FastMCP("trails")
    for operation, mark in marked:
        if mark.toolset not in served:
            continue
        bound = context.bind(operation, lambda: db_path)
        server.tool(
            _translating(bound),
            name=ops.tool_name(operation),
            output_schema=_output_schema(operation),
            annotations=ToolAnnotations(
                readOnlyHint=mark.read_only,
                destructiveHint=mark.destructive,
                idempotentHint=mark.idempotent,
            ),
        )
    return server


def _resolved_toolset(toolset: Toolset | None) -> Toolset:
    """The argument when given, else `$TRAILS_MCP_TOOLSET`, else "agent".

    An unrecognised environment value is refused rather than read as "agent": a host that
    asked for the full toolset and silently got the default would see tools missing with no
    reason given.
    """
    if toolset is not None:
        if toolset not in {"agent", "full"}:
            # The Literal annotation binds nothing at runtime, and a Python caller passing
            # "bogus" would otherwise get the default toolset with no reason given.
            raise ValueError(f"toolset must be 'agent' or 'full', not {toolset!r}")
        return toolset
    raw = os.environ.get(TOOLSET_ENV, "").strip()
    if raw in {"", "agent"}:
        return "agent"
    if raw == "full":
        return "full"
    raise ValueError(f"{TOOLSET_ENV}={raw!r} is not a toolset; use agent or full")


def operation_mark(operation: Any) -> Operation:
    """The operation's `OPERATION` mark, read off its module."""
    mark = getattr(sys.modules[operation.__module__], "OPERATION", None)
    if not isinstance(mark, Operation):
        raise TypeError(
            f"operation {ops.operation_name(operation)} has no OPERATION = Operation(...) in "
            f"{operation.__module__}; every operation declares its toolset and hints, so that "
            "a new operation cannot land in the default MCP toolset unmarked"
        )
    return mark


def _translating(bound: Any) -> Any:
    """Return a refusal as data, and a RenderedResult as its text plus the model.

    Preserves everything fastmcp reads off the function.
    """

    @wraps(bound)
    async def called(*args: Any, **kwargs: Any) -> Any:
        token = sessions.CLIENT_NAME.set(_client_name())
        try:
            result = await bound(*args, **kwargs)
        except TrailsError as exc:
            return ToolResult(structured_content={"error": str(exc)}, is_error=True)
        finally:
            sessions.CLIENT_NAME.reset(token)
        if isinstance(result, RenderedResult):
            # The text an agent reads, and the model a host can parse, from one result.
            return ToolResult(
                content=[TextContent(type="text", text=result.render_text())],
                structured_content=result.model_dump(mode="json"),
            )
        return result

    return called


def _client_name() -> str | None:
    """The MCP client's name from its `initialize` request, which decides the harness a write
    is attributed to (`sessions`). None when the request context carries none."""
    try:
        params = get_context().session.client_params
    except Exception:  # no request context, or a transport without client params
        return None
    info = getattr(params, "clientInfo", None) if params is not None else None
    name = getattr(info, "name", None)
    return name if isinstance(name, str) and name else None


def _output_schema(operation: Any) -> dict[str, Any]:
    """The operation's model, or a refusal.

    An object with `anyOf` branches rather than a top-level `anyOf`, and rather than the derived
    model schema alone. Measured on fastmcp 3.4.7 and the mcp SDK: with the derived schema
    alone, structured content is validated against it and an `{"error": ...}` payload comes back
    as a PROTOCOL error, which is exactly what errors-as-data exists to avoid; a top-level
    `anyOf` is refused outright, because MCP requires an object output schema. This shape keeps
    the model's own required fields for a successful call while admitting the refusal.

    `$defs` is HOISTED to the top level rather than left inside the branch. A model that nests
    another puts its definitions in its own `$defs` and refers to them as `#/$defs/X`, and a
    `$ref` resolves from the DOCUMENT ROOT — so nesting the model schema inside `anyOf` leaves
    the definitions one level too deep and no ref resolves. Measured: a populated nested
    response then comes back as a protocol error with no structured content, while the SAME
    operation returning an empty nested list succeeds, because no `$ref` is instantiated —
    which is why a characterization test over a nested type cannot see this and only a test
    over a populated nested value can. For a flat model there is no `$defs` to pop and the
    result is unchanged.
    """
    model = get_type_hints(operation, include_extras=True)["return"]
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", None)
    output: dict[str, Any] = {"type": "object", "anyOf": [schema, ERROR_SCHEMA]}
    if definitions:
        output["$defs"] = definitions
    return output
