"""Command-line entry point for trails.

ONE DECLARATION, TWO SURFACES. This module owns nothing but the console-script entry
point: the app is built by `trails.surfaces.cli.build_cli`, which registers every
operation in `trails.ops.operations()` with cyclopts. There is no command list here
and no per-command anything — an operation is added by adding a module under
`trails/ops/`, which registers it on this surface and on the MCP surface at once,
because nothing offers the option of registering on one.

MUST NOT IMPORT FASTMCP, directly or transitively. Measured: `import fastmcp` costs 480ms
against 62ms for `import cyclopts`, and this module is imported on every `trails`
invocation. A test asserts it in a subprocess.

It prints each result's text, or its JSON with `--json`.
"""

from __future__ import annotations

from trails.surfaces.cli import build_cli

app = build_cli()


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
