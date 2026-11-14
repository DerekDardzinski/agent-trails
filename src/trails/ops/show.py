"""The `show` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.errors import UsageError
from trails.ops import context
from trails.ops._common import shown_entries
from trails.ops._entry_types import ShownProject, ShowResult
from trails.ops._marks import Operation
from trails.render import call
from trails.vocab import PROJECT_ID_PATTERN

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def show(
    id: Annotated[  # noqa: A002
        str | list[str],
        Field(description="One entry or project id, or a list mixing both."),
    ],
    versions: Annotated[bool, Field(description="Also list every version.")] = False,
    comments: Annotated[int, Field(description="How many of the newest comments to show.")] = 5,
    since_comment: Annotated[
        str | None,
        Field(
            description=(
                "A comment id on this entry: also list the versions that changed and the "
                "links added or removed since it, under a task too."
            )
        ),
    ] = None,
) -> ShowResult:
    """Read entries or projects by exact id: the current version, derived state, links with the versions they were written against, the newest comments, task events and provenance.

    Use this when: search, prime or ready gave you an id, or before writing a new version, to get the base_version.

    Use instead: search, to find entries by meaning; ready, for the next task.

    Returns: each item in request order; for an entry its state as stored facts, links ("written against vN, now vM" when the target moved on), comments with total and the paging call, task events, the harness, session, transcript and agent that wrote the current version, and the tasks the creating session held claims on when the entry was created. Missing ids are named.
    """
    if comments < 0:
        raise UsageError(f"comments must be 0 or more, not {comments}")
    ids = [id] if isinstance(id, str) else list(id)
    ids = list(dict.fromkeys(ids))
    store = context.store()
    project_ids = [item for item in ids if PROJECT_ID_PATTERN.fullmatch(item)]
    entry_ids = [item for item in ids if item not in project_ids]
    found = store.get_entries(entry_ids)
    entries = shown_entries(
        store,
        [found[item] for item in entry_ids if item in found],
        comments=comments,
        versions=versions,
        since_comment=since_comment,
    )
    projects = []
    for item in project_ids:
        project = store.get_project(item)
        if project is None:
            continue
        framing = project.current_framing
        projects.append(
            ShownProject(
                id=project.id,
                name=project.name,
                paths=project.paths,
                framing_title=framing.title if framing else None,
                framing_body=framing.body if framing else None,
                framing_version=framing.version if framing else None,
            )
        )
    present = {item.id for item in entries} | {item.id for item in projects}
    warnings: list[str] = []
    if since_comment is not None:
        on = store.get_comment(since_comment).entry_id
        if on not in {item.id for item in entries}:
            warnings.append(
                f"since_comment {since_comment} is on {on}, which this call does not show, so "
                f"no changes are listed; read them with "
                f"{call('show', on, since_comment=since_comment)}"
            )
    return ShowResult(
        warnings=warnings,
        entries=entries,
        projects=projects,
        order=[item for item in ids if item in present],
        missing=[item for item in ids if item not in present],
    )
