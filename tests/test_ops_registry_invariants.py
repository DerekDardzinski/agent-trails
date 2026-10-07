"""Invariants that hold of the REGISTRY rather than of any one operation.

Each property here is invisible to a family test: a family suite proves its own operations
and cannot see that an operation is missing, that a vocabulary drifted from the schema an
agent reads, or that a module reintroduced an import that breaks the binders.

Deliberately NO `from __future__ import annotations`, for the reason these tests assert.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any, get_type_hints

from fastmcp import Client
from pydantic import BaseModel

from trails import ops
from trails.store import Store
from trails.surfaces import mcp as mcp_surface

OPS_ROOT = Path(__file__).resolve().parent.parent / "src" / "trails" / "ops"


def tools(store: Store) -> dict[str, Any]:
    """Every tool the built server exposes, with its input and output schema."""
    server = mcp_surface.build_mcp_server(store.db_path, "full")

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            return {
                tool.name: {"input": tool.inputSchema, "output": tool.outputSchema}
                for tool in await client.list_tools()
            }

    return asyncio.run(go())


def test_no_operation_signature_carries_a_transport_parameter(store: Store) -> None:
    """The store, `--db` and every from-file twin are synthesized by the binder.

    Silent failure: a transport affordance in the MCP schema is a parameter the agent cannot
    usefully fill and will try to — told to pass a path to a prose field, it would have that
    path recorded as the prose.
    """
    for operation in ops.operations():
        names = set(inspect.signature(operation).parameters)
        offenders = {
            name for name in names if name in {"store", "db"} or name.endswith("_from_file")
        }
        assert offenders == set(), f"{ops.operation_name(operation)} carries {offenders}"

    for name, schemas in tools(store).items():
        properties = set(schemas["input"].get("properties", {}))
        offenders = {
            key for key in properties if key in {"store", "db"} or key.endswith("_from_file")
        }
        assert offenders == set(), f"{name} declares {offenders}"


def test_no_module_under_ops_uses_future_annotations() -> None:
    """Measured in phase 1, and it contradicts the convention every other module follows.

    Silent failure: with it, annotations become strings that `get_type_hints` re-evaluates per
    call, building a fresh pydantic `FieldInfo` each time — and `FieldInfo` has no value
    equality, so the type-hint equality the binders depend on fails even comparing a function
    with itself. Because the rest of the repo carries that import as a convention, it will be
    added back by reflex, and the failure it causes points at the binder rather than at the
    import.
    """
    offenders = [
        path.name
        for path in sorted(OPS_ROOT.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]

    assert offenders == []


def test_every_operation_returns_a_concrete_basemodel(store: Store) -> None:
    """Rule 5, across the complete set rather than against a probe.

    Silent failure: fastmcp wraps a bare union, an optional model or a scalar in a synthetic
    `{"result": ...}` envelope that the CLI binder does not produce, so the two surfaces emit
    different objects for the same call. `ops.operations()` raises TypeError at discovery, so
    the assertion here is that discovery SUCCEEDS for every real operation, not only for
    phase 1's probes.
    """
    operations = ops.operations()

    for operation in operations:
        returned = get_type_hints(operation, include_extras=True)["return"]
        assert isinstance(returned, type) and issubclass(returned, BaseModel), ops.operation_name(
            operation
        )


def test_the_full_toolset_serves_every_operation_not_marked_cli(store: Store) -> None:
    """An operation is withheld from MCP only by its own mark, never by the binder.

    Silent failure: the divergence this epic removes at its root — four operations had no MCP
    tool at all, and nothing in either surface's code said so. Both sets are derived from the
    one registry here, so the assertion is that the derivation covers every operation the marks
    allow and that the two spellings agree.
    """
    served = [fn for fn in ops.operations() if mcp_surface.operation_mark(fn).toolset != "cli"]

    assert {ops.tool_name(fn) for fn in served} == set(tools(store))
