"""trails-mcp — the MCP server over the trails store, served on stdio.

ONE DECLARATION, TWO SURFACES. This module owns nothing but the console-script entry
point: the server is built by `trails.surfaces.mcp.build_mcp_server`, which registers
the operations in `trails.ops.operations()` as tools according to each one's toolset mark.
There is no tool list here and no per-tool anything — an operation is added by adding a
module under `trails/ops/`, which registers it here and on the CLI at once.

`--toolset full` serves the optional toolset as well as the default one, and wins over
`$TRAILS_MCP_TOOLSET`. Operations marked "cli" are never served here.

The operation docstrings and parameter descriptions are the load-bearing part of this
surface. They are what an agent reads to decide WHEN to reach for the store, and the
store's whole value depends on being consulted at the moment a decision is made rather
than at the start of a session — a session-start query is untargeted, while a query at
"I am about to propose this belief" has the subject in hand. So each docstring names the
decision it serves, and each parameter is described ONCE, in its `Field(description=...)`,
which reaches this surface's JSON schema and the CLI's help from the same string.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from trails.surfaces.mcp import TOOLSET_ENV, build_mcp_server


def main(argv: list[str] | None = None) -> None:
    """Entry point for `trails-mcp`. Serves over stdio."""
    parser = argparse.ArgumentParser(prog="trails-mcp", description="trails MCP server (stdio)")
    parser.add_argument(
        "--toolset",
        choices=["agent", "full"],
        default=None,
        help=f"agent (default) or full; overrides ${TOOLSET_ENV}",
    )
    arguments = parser.parse_args(argv)
    raw = os.environ.get("TRAILS_DB", "")
    build_mcp_server(Path(raw) if raw else None, arguments.toolset).run(transport="stdio")
