"""The boundary between the operations and the two surfaces, read off the BUILT surfaces.

This file used to hold a hardcoded `COMMANDS` set and a hardcoded `TOOLS` set — two
independent lists of what exists, which is the divergence pattern this package's surfaces
were rebuilt to remove. Both are gone. Every assertion here is derived from
`ops.operations()` and from what the two entry points actually expose.

Three properties, and each is invisible to the other two:

1. The two entry points expose the same operations. Nothing else in the suite builds the
   CLI, so nothing else would notice an entry point still pointing at a package that a
   stale environment happens to keep.
2. One operation body per module, which is what keeps two builders adding operations out of
   one file.
3. No per-operation branch inside either binder. A binder that gains one keeps every name
   and every signature intact and makes exactly one operation behave differently on the two
   surfaces, which is the divergence class this epic removes reappearing in the one place
   nothing else looks.

Deliberately NO `from __future__ import annotations`: the parameter assertion reads
`get_type_hints` off the built commands, and the reason it must stay off is recorded in
`src/trails/ops/__init__.py`.
"""

import ast
import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import cyclopts
from fastmcp import Client

from trails import cli, ops
from trails.ops._marks import FromFile
from trails.store import Store
from trails.surfaces import mcp as mcp_surface

SURFACES_ROOT = Path(__file__).resolve().parent.parent / "src" / "trails" / "surfaces"
SRC_ROOT = SURFACES_ROOT.parent

# The parameters the CLI binder synthesizes and the operations never declare. Anything
# else on a command comes from the operation's own signature.
SYNTHESIZED = {"db", "json"}


def _leaves(app: cyclopts.App, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Every leaf command in the built app, as its path and its registered function.

    A sub-app with no sub-commands of its own is a leaf; `--help` and friends are skipped
    because they are cyclopts' own flags rather than registered operations.
    """
    children = {
        name: sub for name, sub in getattr(app, "_commands", {}).items() if not name.startswith("-")
    }
    if not children:
        return [(prefix, app.default_command)]
    return [pair for name, sub in children.items() for pair in _leaves(sub, (*prefix, name))]


def _tool_names(store: Store, toolset: mcp_surface.Toolset | None = None) -> set[str]:
    server = mcp_surface.build_mcp_server(store.db_path, toolset)

    async def go() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(go())


def _tool_properties(store: Store) -> dict[str, set[str]]:
    server = mcp_surface.build_mcp_server(store.db_path, "full")

    async def go() -> dict[str, set[str]]:
        async with Client(server) as client:
            return {
                tool.name: set(tool.inputSchema.get("properties", {}))
                for tool in await client.list_tools()
            }

    return asyncio.run(go())


def test_the_built_cli_and_the_built_server_expose_the_same_operation_set(store: Store) -> None:
    """The two entry points, not two lists, decide what exists.

    Silent failure: an operation reachable on ONE surface only, which is the divergence at the
    root of this epic — four operations had no MCP tool at all and nothing in either surface's
    code said so. It survives here in a new form: an entry point still wired to an old
    package, or a binder that quietly skips an operation whose signature it cannot handle.
    No other test in the suite builds the CLI, so no other test can see either.

    The app is imported from `trails.cli`, which is the module the `trails` console script
    loads, rather than by calling `build_cli` directly. Asserting against the builder would
    pass while the entry point pointed somewhere else entirely.

    The MCP side is compared per toolset, because the server serves an operation only in the
    toolset its mark names: every operation not marked "cli" with toolset full, and only the
    operations marked "agent" by default. The expected sets are derived from the marks, because
    three units add operations in parallel and a literal would conflict at every merge; the
    literal default-toolset check is added after they merge.
    """
    paths = [path for path, _ in _leaves(cli.app)]
    from_cli = {"_".join(path) for path in paths}
    marks = {
        ops.operation_name(fn): mcp_surface.operation_mark(fn).toolset for fn in ops.operations()
    }

    assert from_cli == set(marks)
    assert _tool_names(store, "full") == {name for name, mark in marks.items() if mark != "cli"}
    assert _tool_names(store, "agent") == {name for name, mark in marks.items() if mark == "agent"}


def test_each_operation_body_lives_in_its_own_module() -> None:
    """One operation per module, which is what the boundary is for.

    Silent failure: a module that re-exports a sibling's function passes discovery — the
    registry takes the attribute named after the module and does not care where it was
    defined — registers on both surfaces and reads as entirely correct, while two operation
    bodies share one file and the next two builders collide there.
    """
    defining = {
        ops.operation_name(operation): operation.__module__ for operation in ops.operations()
    }

    assert defining == {name: f"trails.ops.{name}" for name in defining}


def test_neither_binder_branches_on_a_named_operation() -> None:
    """No per-operation special case in either binder.

    Silent failure: a binder gains one `if name == "show"` branch. Every set-equality
    assertion stays green because the name is unchanged, every signature assertion stays
    green because the parameters are unchanged, and that one operation behaves differently on
    the two surfaces. That is the divergence this epic removes, reintroduced in the only place
    that is common to every operation.

    A branch has to IDENTIFY its operation, and a binder receives functions and names only
    through `ops.*`, so there are two routes and both are closed: naming it as a string, or
    importing its module to compare by identity. String CONSTANTS only, not attribute names,
    so an ordinary `dict.update` call cannot fail this for the wrong reason.
    """
    named = {ops.operation_name(fn) for fn in ops.operations()}
    named |= {ops.tool_name(fn) for fn in ops.operations()}
    for fn in ops.operations():
        named |= set(ops.cli_path(fn))

    offenders: dict[str, list[str]] = {}
    imports: dict[str, list[str]] = {}
    for path in sorted(SURFACES_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text())
        constants = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if constants & named:
            offenders[path.name] = sorted(constants & named)
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "") != ""
        ]
        reached = [
            module
            for module in modules
            if module is not None
            and module.removeprefix("trails.ops.").split(".")[0] in named
            and module.startswith("trails.ops.")
        ]
        if reached:
            imports[path.name] = sorted(reached)

    assert offenders == {}
    assert imports == {}


def test_every_operation_accepts_the_same_parameters_on_both_surfaces(store: Store) -> None:
    """One signature reaches both surfaces, with only the transport affordances added.

    Silent failure, and it is the store's most consequential false negative: a parameter one
    surface accepts and drops. An agent that explicitly asked for provisional records is told
    nothing exists, and the store holds the record. This used to need a test parametrized over
    several commands AND several tools, because each surface built and forwarded its own
    arguments; under one declaration it is structural and one assertion covers every operation.

    The CLI carries extra parameters synthesized by the binder and belonging to no operation:
    `db`, `json`, and a `--<name>-from-file` twin for each parameter marked `FromFile`.
    """
    commands = {"_".join(path): function for path, function in _leaves(cli.app)}
    properties = _tool_properties(store)

    for operation in ops.operations():
        name = ops.operation_name(operation)
        declared = set(inspect.signature(operation).parameters)
        twins = {
            f"{parameter}_from_file"
            for parameter, annotation in operation.__annotations__.items()
            if parameter != "return"
            and any(isinstance(item, FromFile) for item in getattr(annotation, "__metadata__", ()))
        }

        assert set(inspect.signature(commands[name]).parameters) == declared | twins | SYNTHESIZED
        if mcp_surface.operation_mark(operation).toolset != "cli":
            assert properties[ops.tool_name(operation)] == declared, name


_ORDER_BY = re.compile(r"\bORDER\s+BY\s+(.*?)(?:\bLIMIT\b|\)|$)", re.IGNORECASE | re.DOTALL)
_FULLTEXT_COLUMNS = {"title", "body"}


def _sorts_only_on_fulltext_column(sql: str) -> bool:
    """True when an ORDER BY in `sql` has one key and that key is title or body."""
    for clause in _ORDER_BY.findall(sql):
        keys = [key.split() for key in clause.split(",") if key.strip()]
        if len(keys) == 1 and keys[0][0].split(".")[-1].strip("`").lower() in _FULLTEXT_COLUMNS:
            return True
    return False


def _sql_fragments(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module, with each f-string's literal parts joined."""
    fragments = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            fragments.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = [
                str(part.value) if isinstance(part, ast.Constant) else " {} "
                for part in node.values
            ]
            fragments.append((node.lineno, "".join(parts)))
    return fragments


def test_no_sql_sorts_by_title_or_body_as_its_only_key() -> None:
    """labbook-79y.6 item 11, Beads memory tooling-dolt-fulltext-order-by. Silent failure: on
    dolt 2.4.0 an ORDER BY whose only key is a FULLTEXT-indexed column returns zero rows, so
    such a query looks like an empty result rather than a failure."""
    assert _sorts_only_on_fulltext_column("SELECT id FROM record AS r ORDER BY r.title DESC")
    assert not _sorts_only_on_fulltext_column("SELECT id FROM record ORDER BY title, id")

    offending = [
        f"{path.relative_to(SRC_ROOT)}:{line}"
        for path in sorted(SRC_ROOT.rglob("*.py"))
        for line, text in _sql_fragments(path)
        if _sorts_only_on_fulltext_column(text)
    ]
    assert offending == []
