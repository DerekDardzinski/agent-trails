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
from typing import Any

import pytest
from fastmcp import Client

from trails import ops
from trails.mcp import server as server_entry
from trails.models import RecordDraft
from trails.ops._marks import Operation
from trails.ops.types import RenderedResult
from trails.store import Store
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


def test_export_is_in_the_default_toolset(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the real registry. Silent failure: export missing from the toolset whose tool
    descriptions tell agents to use it."""
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)

    assert "export" in _tools(store)


def test_the_default_toolset_is_exactly_the_nine_agent_tools(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 9, over the real registry. Silent failure: an operation's mark
    changed so the toolset every agent gets by default grows or loses a tool."""
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)

    assert sorted(_tools(store)) == [
        "export",
        "lineage",
        "link",
        "prime",
        "publish",
        "retire",
        "search",
        "set_work_status",
        "show",
    ]


def test_the_full_toolset_has_register_project_and_no_removed_operations(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure: a removed operation still served, or register_project
    missing from the full toolset."""
    monkeypatch.delenv(mcp_surface.TOOLSET_ENV, raising=False)

    tools = set(_tools(store, "full"))

    assert "register_project" in tools
    assert not {"init_project", "purge", "render"} & tools


def test_an_unrecognised_toolset_argument_is_refused(store: Store) -> None:
    """labbook-79y.6 item 10. Silent failure: a Python caller's misspelled toolset served as
    the default, hiding tools with no reason given."""
    with pytest.raises(ValueError, match="toolset must be 'agent' or 'full', not 'bogus'"):
        mcp_surface.build_mcp_server(store.db_path, "bogus")  # ty: ignore[invalid-argument-type]


def test_a_store_refusal_sets_the_mcp_error_flag_and_keeps_its_message(store: Store) -> None:
    """Silent failure: a client that trusts MCP's error flag treating a refused lookup as a
    successful tool call."""
    server = mcp_surface.build_mcp_server(store.db_path, "agent")

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(
                "show", {"record_id": "rec-0000000000000000"}, raise_on_error=False
            )

    result = asyncio.run(go())

    assert result.is_error is True
    assert result.structured_content == {"error": "no record has id 'rec-0000000000000000'"}
    assert "no record has id" in result.content[0].text


def test_show_array_is_exposed_through_mcp_with_partial_missing_ids(store: Store) -> None:
    """Catches the Python list form working while MCP rejects the array schema or turns a
    partial miss into an error result."""
    record = store.write_record(
        RecordDraft(work_status="closed", title="Found", body="body", entry_type="note")
    ).record
    missing = "rec-0000000000000000"
    server = mcp_surface.build_mcp_server(store.db_path, "agent")

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(
                "show", {"record_id": [missing, record.id], "detail": "full"}
            )

    result = asyncio.run(go())

    assert result.is_error is False
    assert result.structured_content["missing"] == [missing]
    assert result.structured_content["records"][0]["record"]["id"] == record.id
    assert result.content[0].text.startswith("state: 1 records found · 1 not found · detail full")


def test_publish_schema_exposes_entry_type_and_work_status_choices(store: Store) -> None:
    """Catches literal choices degrading to unconstrained strings in the agent tool schema."""
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
            return tools["publish"]["properties"]

    properties = asyncio.run(go())
    assert properties["entry_type"]["anyOf"][0]["enum"] == [
        "project-context",
        "problem-statement",
        "hypothesis",
        "procedure",
        "result",
        "verification",
        "fact",
        "insight",
        "decision",
        "report",
        "note",
    ]
    assert properties["work_status"]["anyOf"][0]["enum"] == ["open", "closed"]


def test_search_schema_explains_work_status_history_behavior(store: Store) -> None:
    """Catches an agent-facing schema that hides the current-only work-status exception or
    fails to say which superseded and retired records include_history restores."""
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
            return tools["search"]["properties"]

    properties = asyncio.run(go())
    assert properties["work_status"]["description"] == (
        "Keep only records whose latest work status is open or closed. Unless include_history "
        "is true, this filter searches current record versions only. Work status is separate "
        "from whether a record is current, superseded or retired."
    )
    assert properties["include_history"]["description"] == (
        "Also return retired records. When work_status is set, also restore superseded versions "
        "that the default current-version work-status search excludes. Searches without "
        "work_status continue to return superseded records and label them."
    )


def test_search_description_distinguishes_keyword_windows_from_semantic_chunks(
    store: Store,
) -> None:
    """Catches the agent-facing tool description retaining the former universal 240-character
    cap after semantic-only hits begin returning complete selected chunks."""
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> str:
        async with Client(server) as client:
            tools = {tool.name: tool.description for tool in await client.list_tools()}
            return tools["search"] or ""

    description = asyncio.run(go())
    assert (
        "A body-keyword match shows a window of at most 240 characters centered on the first match."
    ) in description
    assert (
        "A semantic match with no body-keyword match shows the complete selected embedding chunk."
    ) in description
