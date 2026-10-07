"""The two binders over one set of operations.

This module imports NEITHER binder. `surfaces/cli.py` must not pull fastmcp into a CLI
process — measured, `import fastmcp` costs 480ms against 62ms for `import cyclopts` — and an
import here would do exactly that for anyone importing the package.
"""
