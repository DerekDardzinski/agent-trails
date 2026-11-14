"""How a response spells the next call an agent can make."""

from __future__ import annotations

import json
from typing import Any


def call(operation: str, *positional: Any, **arguments: Any) -> str:
    """`operation(arg, key=value)` with JSON values, omitting empty and false keywords, so a
    hint can be pasted back as a call."""
    parts = [_value(value) for value in positional]
    parts += [
        f"{key}={_value(value)}"
        for key, value in arguments.items()
        if value is not None and value is not False and value not in ("", [])
    ]
    return f"{operation}({', '.join(parts)})"


def next_line(hints: list[str]) -> str:
    return "Next: " + " · ".join(hints)


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
