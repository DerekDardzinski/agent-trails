"""The `export` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails.errors import ValidationError
from trails.ops import context
from trails.ops._maint_types import ExportResult
from trails.ops._marks import Operation
from trails.record_file import to_markdown

# destructive: with overwrite=true it replaces an existing file.
OPERATION = Operation(toolset="agent", read_only=False, destructive=True, idempotent=True)


async def export(
    record_id: Annotated[str, Field(description="The record to export.")],
    path: Annotated[str, Field(description="File to write. Parent directories are created.")],
    overwrite: Annotated[bool, Field(description="Replace an existing file at path.")] = False,
) -> ExportResult:
    """Write a record to a Markdown file with a TOML header, so you can edit it with your file tools and publish it back with publish(from_file=path) after adding a reason line to the header.

    Use this when: you need to change part of a long record, such as one step of a procedure.

    Use instead: show, to only read it.

    Returns: the path written. Refuses an existing file unless overwrite is true.
    """
    record = context.store().get_record(record_id)
    target = Path(path).expanduser().resolve()
    if target.exists() and not overwrite:
        raise ValidationError(f"{target} exists; pass overwrite=true to replace it")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_markdown(record))
    except OSError as exc:
        raise ValidationError(f"cannot write {target}: {exc}") from exc
    return ExportResult(record_id=record.id, path=str(target))
