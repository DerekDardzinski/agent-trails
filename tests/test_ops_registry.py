"""Tests for the registry both binders build from, and for the shared parameter aliases.

Deliberately NO `from __future__ import annotations`, for the reason recorded in
`tests/test_ops_context.py` and in `src/trails/ops/__init__.py`.
"""

from typing import Annotated, Any, get_args

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from trails import ops
from trails.ops._marks import FromFile
from trails.ops.types import OpFn


class Good(BaseModel):
    id: str


class Other(BaseModel):
    id: str


async def good() -> Good:
    """Return a concrete model."""
    return Good(id="x")


async def bare_union() -> Good | Other:
    """Return a bare union."""
    return Good(id="x")


async def optional_model() -> Good | None:
    """Return an optional model."""
    return None


async def scalar() -> str:
    """Return a scalar."""
    return "x"


async def anything() -> Any:
    """Return anything."""
    return None


async def write_goal(title: Annotated[str, Field(description="a title")]) -> Good:
    """Record a goal."""
    return Good(id=title)


async def show(record_id: Annotated[str, Field(description="an id")]) -> Good:
    """Read one record."""
    return Good(id=record_id)


def test_no_helper_module_is_discovered_as_an_operation() -> None:
    """Catches a helper module being discovered as an operation.

    `types` and `context` sit at contracted names and carry no operation, so a discovery rule
    that only skipped leading underscores would register two commands nobody wrote and fail
    on the missing attribute.

    This asserted an EMPTY registry while the foundation phase had ported no operations. The
    premise expired when they were ported; the property it was written to catch did not, so
    the assertion moved to the property and the reasoning above is unchanged.
    """
    discovered = ops.operation_module_names()

    assert "types" not in discovered
    assert "context" not in discovered
    assert not [name for name in discovered if name.startswith("_")]


@pytest.mark.parametrize(
    "operation",
    [bare_union, optional_model, scalar, anything],
    ids=["bare-union", "optional-model", "scalar", "any"],
)
def test_operations_rejects_every_return_type_that_is_not_a_concrete_basemodel(
    operation: OpFn[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four distinct rejected shapes, refused where BOTH binders build from.

    fastmcp wraps a non-object return in a synthetic `{"result": ...}` envelope the CLI binder
    does not produce, so the two surfaces would emit different objects for the same call. The
    registry is heterogeneous and therefore typed `OpFn[..., Any]`, so no type checker can
    catch this: enforcing at discovery is what makes the import fail instead.
    """
    monkeypatch.setattr(ops, "_discovered", lambda: (operation,))

    with pytest.raises(TypeError) as raised:
        ops.operations()

    assert ops.operation_name(operation) in str(raised.value)


def test_operations_accepts_a_concrete_basemodel_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-strict check rejects every real operation and has to be removed to ship.

    That is how a structural rule quietly becomes a comment, so the accepting case is asserted
    beside the rejecting ones.
    """
    monkeypatch.setattr(ops, "_discovered", lambda: (good,))

    assert ops.operations() == (good,)


def test_operation_name_maps_one_canonical_name_to_both_spellings() -> None:
    """A name spelled two ways puts a command at one address and its tool at another.

    That is the divergence class this layer removes, so both derived spellings come from the
    one canonical name and are asserted against hardcoded literals.
    """
    assert ops.operation_name(write_goal) == "write_goal"
    assert ops.cli_path(write_goal) == ("write", "goal")
    assert ops.tool_name(write_goal) == "write_goal"

    assert ops.operation_name(show) == "show"
    assert ops.cli_path(show) == ("show",)
    assert ops.tool_name(show) == "show"


def test_the_from_file_mark_rides_in_the_metadata_and_pydantic_ignores_it() -> None:
    """The mark must be readable by the CLI binder and invisible to the schema.

    Pydantic raising on unknown metadata, or folding it into a constraint, would put a
    transport affordance into the MCP schema — which is asserted end to end against a live
    server elsewhere; this pins the mechanism the binder depends on.
    """
    marked = Annotated[str | None, Field(description="prose"), FromFile()]

    assert FromFile() in get_args(marked)
    assert TypeAdapter(marked).json_schema() == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "prose",
    }
