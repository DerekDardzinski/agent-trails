"""The operations, discovered from this package and validated at discovery.

One module per operation, each exposing exactly one async function whose name matches the
module. THE SIGNATURE IS THE DECLARATION: there is no dataclass mirror, no schema file, no
code generation and no artifact on disk.

There is no per-operation surface choice. A caller registers ALL of these with BOTH binders,
which is what makes divergence structural: an operation cannot exist on one surface only
because nothing offers the option.

NO MODULE IN THIS PACKAGE USES `from __future__ import annotations`. With it, annotations
are strings that `get_type_hints` re-evaluates on every call, and pydantic's `FieldInfo` has
no value equality — so `get_type_hints(fn) != get_type_hints(fn)` for the same function, and
the equality that `bind` must preserve becomes unsatisfiable. Without it the annotation
objects are built once at definition time and the wrapper shares them.
"""

import importlib
import pkgutil
from typing import Any, get_type_hints

from pydantic import BaseModel

from trails.ops.types import OpFn

# The two modules contracted to sit at these names and carry no operation. Everything else
# in the package that does not start with `_` is an operation module.
_MODULES_WITHOUT_AN_OPERATION = frozenset({"types", "context"})


def operation_module_names() -> tuple[str, ...]:
    """Every operation module in this package, sorted.

    Both binders register in this order, so it is identical everywhere. Names starting with
    `_` are helpers.
    """
    return tuple(
        sorted(
            info.name
            for info in pkgutil.iter_modules(__path__)
            if not info.name.startswith("_") and info.name not in _MODULES_WITHOUT_AN_OPERATION
        )
    )


def _discovered() -> tuple[OpFn[..., Any], ...]:
    """Import every operation module and take the function named after it.

    Taking the matching attribute rather than scanning for callables is what makes a
    misspelled function fail loudly: the alternative is an operation that is simply absent
    from both surfaces, which reads as a command nobody wrote.
    """
    operations = []
    for name in operation_module_names():
        module = importlib.import_module(f"{__name__}.{name}")
        operations.append(getattr(module, name))
    return tuple(operations)


def _validated(operations: tuple[OpFn[..., Any], ...]) -> tuple[OpFn[..., Any], ...]:
    """Refuse any operation whose return annotation is not a concrete BaseModel subclass.

    Rejects a bare union, an optional model, a scalar and `Any` alike. fastmcp wraps a
    non-object return in a synthetic `{"result": ...}` envelope that the CLI binder does not
    produce, so such an operation would make the two surfaces emit different objects for the
    same call — the exact class of defect this layer removes.

    A test is not sufficient on its own here: the registry is heterogeneous, so it is typed
    `OpFn[..., Any]` and a type checker cannot check return types across it. Enforcing at
    discovery makes the import fail instead of one surface disagreeing with the other.
    """
    for fn in operations:
        returned = get_type_hints(fn, include_extras=True).get("return")
        if not (isinstance(returned, type) and issubclass(returned, BaseModel)):
            raise TypeError(
                f"operation {operation_name(fn)} returns {returned!r}; every operation must "
                "return a top-level pydantic BaseModel, never a bare union, an optional model "
                "or a scalar, because fastmcp wraps those in a synthetic {'result': ...} "
                "envelope that the CLI binder does not produce"
            )
    return operations


def operations() -> tuple[OpFn[..., Any], ...]:
    """Every operation function, sorted, validated at discovery.

    Empty until the porting phases add modules.
    """
    return _validated(_discovered())


def operation_name(fn: OpFn[..., Any]) -> str:
    """The canonical name, from which each binder derives its own spelling.

    A module named `add_comment` yields CLI path `add comment` and tool name
    `add_comment`; a module named `show` yields `show` and `show`. One mapping, so a name
    cannot be spelled two ways.
    """
    # `OpFn` is a Callable alias, which a type checker models structurally and therefore
    # without `__name__`. Every operation is a module-level async function, so the attribute
    # is always there.
    return fn.__name__  # ty: ignore[unresolved-attribute]


def cli_path(fn: OpFn[..., Any]) -> tuple[str, ...]:
    """The CLI command path: the canonical name split into its segments.

    `add_comment` becomes ("add", "comment"), so `trails add comment` is the command,
    and `show` becomes ("show",).
    """
    return tuple(operation_name(fn).split("_"))


def tool_name(fn: OpFn[..., Any]) -> str:
    """The MCP tool name: the canonical name, unprefixed.

    UNPREFIXED because the Next: hints every response prints spell a call as `show("<id>")`,
    and a hint naming a tool the server does not serve is a dead end. A host that needs a
    namespace adds its own from the server name, `trails`.
    """
    return operation_name(fn)
