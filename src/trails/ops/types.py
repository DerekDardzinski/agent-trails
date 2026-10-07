"""The shared operation types, defined once.

Every operation is an async function, and every edge that passes one around uses `OpFn`.
`bind` preserves BOTH the parameter list and the return type, which is what lets a type
checker see an operation's call sites through the wrapper.

Every operation returns a `RenderedResult`. Both surfaces print `render_text()`: the CLI by
default, and MCP as the text content beside the model as structured content.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type OpFn[**P, R] = Callable[P, Awaitable[R]]


class RenderedResult(BaseModel):
    """An operation result that renders itself as text for an agent to read."""

    def render_text(self) -> str:
        """The text form. Default: the model as indented JSON; every operation overrides it."""
        return self.model_dump_json(indent=2)
