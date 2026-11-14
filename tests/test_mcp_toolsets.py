"""The MCP server serves each operation by its toolset mark, with the mark's safety hints.

Probe operations live in throwaway modules built here, one per mark, because the binder reads
an operation's mark off its module.

Deliberately NO `from __future__ import annotations`: the probes are bound through
`context.bind`, whose signature equality it would break (see `src/trails/ops/__init__.py`).
"""

import asyncio
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from mcp.types import Implementation

from trails import ops
from trails.entry_store import EntryStore as Store
from trails.mcp import server as server_entry
from trails.models import EntryDraft
from trails.ops._marks import Operation
from trails.ops.types import RenderedResult
from trails.surfaces import mcp as mcp_surface


class Probe(RenderedResult):
    name: str

    def render_text(self) -> str:
        return self.name


def _probe_module(name: str, mark: Operation | None) -> types.ModuleType:
    """A module holding one async operation named `name`, marked with `mark` when given."""
    module = types.ModuleType(f"tests._toolset_probes.{name}")

    async def operation() -> Probe:
        """Probe."""
        return Probe(name=name)

    operation.__name__ = name
    operation.__qualname__ = name
    operation.__module__ = module.__name__
    setattr(module, name, operation)
    if mark is not None:
        module.OPERATION = mark  # ty: ignore[unresolved-attribute]
    sys.modules[module.__name__] = module
    return module


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Register three probes: one per toolset, each with distinct hints."""
    marks = {
        "agent_probe": Operation(
            toolset="agent", read_only=True, destructive=False, idempotent=True
        ),
        "full_probe": Operation(
            toolset="full", read_only=False, destructive=False, idempotent=True
        ),
        "cli_probe": Operation(toolset="cli", read_only=False, destructive=True, idempotent=False),
    }
    modules = [_probe_module(name, mark) for name, mark in marks.items()]
    monkeypatch.setattr(
        ops, "operations", lambda: tuple(getattr(m, m.__name__.rsplit(".")[-1]) for m in modules)
    )
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)
    yield
    for module in modules:
        sys.modules.pop(module.__name__, None)


def _tools(store: Store, toolset: mcp_surface.Toolset | None = None) -> dict[str, Any]:
    server = mcp_surface.build_mcp_server(store.db_path, toolset)

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            return {tool.name: tool.annotations for tool in await client.list_tools()}

    return asyncio.run(go())


def test_the_default_toolset_serves_only_agent_operations(store: Store, probes: None) -> None:
    """Silent failure: a full or cli operation leaking into the small default toolset."""
    assert set(_tools(store)) == {"agent_probe"}


def test_the_full_toolset_adds_full_operations_and_never_cli_ones(
    store: Store, probes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: the environment variable ignored, or a cli-only operation (purge)
    becoming reachable over MCP."""
    assert set(_tools(store, "full")) == {"agent_probe", "full_probe"}
    monkeypatch.setenv(mcp_surface.TOOLSET_ENV, "full")
    assert set(_tools(store)) == {"agent_probe", "full_probe"}


def test_the_argument_wins_over_the_environment(
    store: Store, probes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: `--toolset agent` overridden by a stale TRAILS_MCP_TOOLSET=full."""
    monkeypatch.setenv(mcp_surface.TOOLSET_ENV, "full")

    assert set(_tools(store, "agent")) == {"agent_probe"}


def test_an_unrecognised_environment_toolset_is_refused(
    store: Store, probes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: a misspelled `ful` read as the default, hiding tools with no reason."""
    monkeypatch.setenv(mcp_surface.TOOLSET_ENV, "ful")

    with pytest.raises(ValueError, match="TRAILS_MCP_TOOLSET='ful'"):
        mcp_surface.build_mcp_server(store.db_path)


def test_each_tool_carries_the_hints_of_its_mark(store: Store, probes: None) -> None:
    """Silent failure: hints dropped or swapped, so a host treats a write as a read."""
    annotations = _tools(store, "full")

    assert (
        annotations["agent_probe"].readOnlyHint,
        annotations["agent_probe"].destructiveHint,
        annotations["agent_probe"].idempotentHint,
    ) == (True, False, True)
    assert (
        annotations["full_probe"].readOnlyHint,
        annotations["full_probe"].destructiveHint,
        annotations["full_probe"].idempotentHint,
    ) == (False, False, True)


def test_an_unmarked_operation_fails_server_construction_naming_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: a new operation with no mark landing in some toolset by default."""
    module = _probe_module("unmarked_probe", None)
    monkeypatch.setattr(ops, "operations", lambda: (module.unmarked_probe,))
    try:
        with pytest.raises(TypeError, match="operation unmarked_probe has no OPERATION"):
            mcp_surface.build_mcp_server(store.db_path, "full")
    finally:
        sys.modules.pop(module.__name__, None)


def test_the_entry_point_passes_its_toolset_flag(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: `trails-mcp --toolset full` parsed and then not passed to the server."""
    seen: list[Any] = []

    class Stub:
        def run(self, transport: str) -> None:
            seen.append(transport)

    def build(db_path: Any, toolset: Any) -> Stub:
        seen.append(toolset)
        return Stub()

    monkeypatch.setattr(server_entry, "build_mcp_server", build)
    server_entry.main(["--toolset", "full"])
    server_entry.main([])

    assert seen == ["full", "stdio", None, "stdio"]


def test_the_default_toolset_is_exactly_the_agent_tools(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 9, over the real registry. Silent failure: an operation's mark
    changed so the toolset every agent gets by default grows or loses a tool, or a removed
    operation is still served."""
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)

    assert sorted(_tools(store)) == [
        "add_comment",
        "claim",
        "close",
        "link",
        "prime",
        "ready",
        "register_project",
        "release",
        "reopen",
        "retire",
        "search",
        "set_projects",
        "show",
        "unlink",
        "update_project",
        "write",
    ]


def test_the_full_toolset_has_register_project_and_no_removed_operations(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: a removed operation still served, or register_project
    missing from the full toolset."""
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)

    tools = set(_tools(store, "full"))

    assert "register_project" in tools
    assert {"alias", "embed", "lint", "version"} <= tools
    assert not {"publish", "export", "lineage", "mark_verified", "set_work_status"} & tools


def test_an_unrecognised_toolset_argument_is_refused(store: Store) -> None:
    """labbook-79y.6 item 10. Silent failure: a Python caller's misspelled toolset served as
    the default, hiding tools with no reason given."""
    with pytest.raises(ValueError, match="toolset must be 'agent' or 'full', not 'bogus'"):
        mcp_surface.build_mcp_server(store.db_path, "bogus")  # ty: ignore[invalid-argument-type]


def test_a_store_refusal_sets_the_mcp_error_flag_and_keeps_its_message(store: Store) -> None:
    """Silent failure: a client that trusts MCP's error flag treating a refused write as a
    successful tool call."""
    server = mcp_surface.build_mcp_server(store.db_path, "agent")

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(
                "claim", {"id": "tsk-zzzzz", "base_version": 1}, raise_on_error=False
            )

    result = asyncio.run(go())

    assert result.is_error is True
    assert result.structured_content == {"error": "no entry has id 'tsk-zzzzz'"}


def test_show_array_is_exposed_through_mcp_with_partial_missing_ids(store: Store) -> None:
    """Catches the Python list form working while MCP rejects the array schema or turns a
    partial miss into an error result."""
    entry = store.write_entry(EntryDraft(kind="note", title="Found", body="body")).entry
    missing = "note-zzzzz"
    server = mcp_surface.build_mcp_server(store.db_path, "agent")

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool("show", {"id": [missing, entry.id]})

    result = asyncio.run(go())

    assert result.is_error is False
    assert result.structured_content["missing"] == [missing]
    assert result.structured_content["entries"][0]["id"] == entry.id
    assert "\n\nnot found: note-zzzzz\n\n" in result.content[0].text


def test_write_schema_exposes_kind_and_stated_by_choices(store: Store) -> None:
    """Catches literal choices degrading to unconstrained strings in the agent tool schema."""
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
            return tools["write"]["properties"]

    properties = asyncio.run(go())
    assert properties["kind"]["anyOf"][0]["enum"] == [
        "goal",
        "task",
        "note",
        "claim",
        "preference",
        "skill",
    ]
    assert properties["stated_by"]["anyOf"][0]["enum"] == ["agent", "person"]


def test_an_mcp_write_records_the_harness_its_client_names(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: the MCP client name never reaching session detection, so every
    MCP write is attributed by the process tree alone, which for Claude Code launched from a
    Kiro shell names the wrong harness and session (trails fac-ahxap)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session-1")
    monkeypatch.setenv("KIRO_SESSION_ID", "sess_inherited")
    server = mcp_surface.build_mcp_server(store.db_path, "agent")

    async def go() -> Any:
        async with Client(
            server, client_info=Implementation(name="claude-code", version="2.1.325")
        ) as client:
            return await client.call_tool("write", {"kind": "note", "title": "from claude"})

    result = asyncio.run(go())
    written = store.get_entry(result.structured_content["id"]).current
    assert (written.harness, written.session_id) == ("claude", "claude-session-1")


def test_project_tools_separate_creation_from_mutation(store: Store) -> None:
    """Catches update parameters leaking back into registration or update_project losing them."""
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> dict[str, set[str]]:
        async with Client(server) as client:
            tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
            return {
                name: set(tools[name]["properties"])
                for name in ("register_project", "update_project")
            }

    properties = asyncio.run(go())
    assert properties["register_project"] == {"name", "paths", "framing"}
    assert properties["update_project"] == {
        "project",
        "paths",
        "remove_paths",
        "framing",
        "base_version",
        "reason",
    }
