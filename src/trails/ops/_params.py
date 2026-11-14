"""Parameter declarations the create and revise operations share, each with its description,
so one field reads the same in every operation that takes it.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated, Any

from pydantic import Field

from trails.models import ArtifactRef, RepoCapture
from trails.ops._marks import FromFile
from trails.vocab import PRIORITY_RANGE, TASK_ID

Title = Annotated[str, Field(description="One line saying what this is.")]
Body = Annotated[str, Field(description="The content, in Markdown."), FromFile()]
Tags = Annotated[
    list[str], Field(description="Descriptive labels such as bug, goal or a dataset name.")
]
Projects = Annotated[
    list[str],
    Field(description="Project ids or exact names this belongs to; [] leaves it unscoped."),
]
Priority = Annotated[
    int | None,
    Field(
        ge=PRIORITY_RANGE[0],
        le=PRIORITY_RANGE[1],
        description="0 (most urgent) to 4; null for no priority.",
    ),
]
TaskId = Annotated[str, Field(pattern=TASK_ID, description="A task id, tsk-...")]
Value = Annotated[
    dict[str, Any] | None,
    Field(description="Structured numbers or fields as a JSON object."),
]
Artifacts = Annotated[
    list[ArtifactRef],
    Field(
        description=(
            'Data references, each {"location": ..., "digest": null or "sha256:<64 lowercase '
            'hex>", "notes": ...}. Data is referenced, not copied; a digest is shown as you '
            "gave it."
        )
    ),
]
Repos = Annotated[
    list[RepoCapture],
    Field(
        description=(
            'Git repositories to capture, each {"path": ..., "exclude": [...], "allow_large": '
            "[...]}: the whole working tree, uncommitted files included, is copied into the "
            "store without touching your files. Pass every repository whose code produced "
            "this note."
        )
    ),
]
BaseVersion = Annotated[
    int, Field(description="The version you read, from show; a stale one is refused.")
]
Reason = Annotated[str, Field(description="One sentence saying what changed and why.")]
NewTitle = Annotated[str | None, Field(description="The new title; omit to keep it.")]
NewBody = Annotated[
    str | None, Field(description="The new content, in Markdown; omit to keep it."), FromFile()
]
NewTags = Annotated[
    list[str] | None, Field(description="The new labels; omit to keep them, [] to clear them.")
]
