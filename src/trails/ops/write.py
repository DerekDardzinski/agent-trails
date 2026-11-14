"""The `write` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from trails import dolt
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.locations import safe_resolve
from trails.models import ArtifactRef, CodeCapture, EntryDraft
from trails.ops import context
from trails.ops._common import capture_code, discard_capture, embed_after_write, redraw
from trails.ops._entry_types import WriteResult
from trails.ops._guidance import CapturedRepo, uncaptured_location_warnings
from trails.ops._marks import FromFile, Operation
from trails.vocab import WORK_KINDS, Kind, StatedBy

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def write(  # noqa: PLR0917 — the public write contract
    kind: Annotated[
        Kind | None,
        Field(
            description=(
                "Required for a new entry; fixed after. goal: one initiative's framing and "
                "success criteria, never claimed. task: something to do. note: your scratch "
                "pad, evidence for other entries. claim: anything stated as true or possibly "
                "true; its standing comes from what backs it. preference: how the person wants "
                "work done, loaded at session start. skill: written only by the offline "
                "consolidation pass, never by an agent in a session."
            )
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(description="One line saying what this is. Required for a new entry."),
    ] = None,
    body: Annotated[str | None, Field(description="The content, in Markdown."), FromFile()] = None,
    tags: Annotated[
        list[str] | None,
        Field(description="Descriptive labels such as bug, feature or a dataset name."),
    ] = None,
    value: Annotated[
        dict[str, Any] | None,
        Field(description="Structured numbers or fields as a JSON object; {} clears it."),
    ] = None,
    artifacts: Annotated[
        list[ArtifactRef] | None,
        Field(
            description=(
                'Data references, each {"location": ..., "digest": null or "sha256:<64 '
                'lowercase hex>", "notes": ...}. Data is referenced, never copied.'
            )
        ),
    ] = None,
    repos: Annotated[
        list[str] | None,
        Field(
            description=(
                "Git repositories whose whole working tree, uncommitted files included, is "
                "captured into the store without touching your files. Pass every repository "
                "whose code produced a claim. [] clears the previous version's captures."
            )
        ),
    ] = None,
    exclude: Annotated[
        list[str], Field(description="Paths relative to a repository's top level to leave out.")
    ] = [],  # noqa: B006 — contracted default; never mutated
    allow_large: Annotated[
        list[str],
        Field(description="Paths relative to a repository's top level to capture though large."),
    ] = [],  # noqa: B006 — contracted default; never mutated
    projects: Annotated[
        list[str] | None,
        Field(
            description=(
                "Project ids or exact names. A new entry with none is unscoped; a preference "
                "with none is global. On a new version, a list replaces the memberships."
            )
        ),
    ] = None,
    priority: Annotated[
        int | None, Field(description="Goals and tasks only: 0 (highest) to 4.")
    ] = None,
    stated_by: Annotated[
        StatedBy | None,
        Field(
            description=(
                "Claims and preferences only: person when you are recording what the person "
                "said, agent (the default) otherwise."
            )
        ),
    ] = None,
    parent: Annotated[
        str | None,
        Field(description="New entries only: the goal or task this is part_of."),
    ] = None,
    waits_on: Annotated[
        list[str],
        Field(
            description=(
                "New entries only: goals or tasks that must be closed completed before this "
                "one is ready. Create dependencies first so their ids exist."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    id: Annotated[  # noqa: A002 — the public parameter name
        str | None,
        Field(description="An existing entry's id, to write its next version."),
    ] = None,
    base_version: Annotated[
        int | None,
        Field(description="With id: the version you read, from show. A stale one is refused."),
    ] = None,
    reason: Annotated[
        str | None, Field(description="With id: one sentence saying what changed and why.")
    ] = None,
) -> WriteResult:
    """Create an entry, or write the next version of one. Every write records the harness, session and transcript that made it; you never pass them.

    Use this when: you plan work (one task per call, in dependency order, so parent and waits_on name ids that exist), record something true or a person's preference, keep a note, or edit any entry. An edit keeps the id: pass id, the base_version you read and a reason; omitted fields keep their values and [] clears a list.

    Use instead: link, to add an edge after creation; claim, close, reopen and release, to change a task's state; add_comment, for feedback that needs a response; retire, when an entry is no longer true.

    Returns: the id and version, the task graph around a goal or task with each entry's state, warnings (a possible secret, a dependency cycle, a link to an entry that cannot be a parent or dependency), and the show call.
    """
    store = context.store()
    creating = id is None
    if creating and kind is None:
        raise ValidationError(
            "a new entry needs kind: goal, task, note, claim, preference or skill"
        )
    draft = EntryDraft(
        kind=kind,
        id=id,
        base_version=base_version,
        reason=reason,
        title=title,
        body=body,
        tags=tags,
        value=value,
        artifacts=artifacts,
        priority=priority,
        stated_by=stated_by,
        projects=projects,
        parent=parent,
        waits_on=waits_on,
    )
    new_id: str | None = None
    ref_name: str | None = None
    summaries = []
    warnings: list[str] = []
    with dolt.exclusive(store.db_path):
        # Every refusal before capture, so a refused write leaves no capture ref or store.
        store.check_entry_draft(draft)
        if repos:
            if creating:
                new_id = store.new_entry_id(kind)  # ty: ignore[invalid-argument-type]
                ref_name = f"{new_id}/v1"
            else:
                current = store.get_entry(str(id)).current.version
                ref_name = f"{id}/v{current + 1}"
            captures, summaries, warnings = capture_code(
                store, ref_name, repos, exclude, allow_large
            )
            draft = draft.model_copy(update={"code": captures})
        elif repos == []:
            if exclude or allow_large:
                raise ValidationError("exclude and allow_large apply to repos; pass repos too")
            draft = draft.model_copy(update={"code": []})
        elif exclude or allow_large:
            raise ValidationError("exclude and allow_large apply to repos; pass repos too")
        try:
            outcome = store.write_entry(draft, new_id=new_id)
        except ValidationError:
            if ref_name is not None and not _landed(store, ref_name, draft.code or []):
                discard_capture(store, ref_name)
            raise
    # THE ENTRY IS DURABLE from here, so nothing below may raise.
    entry = outcome.entry
    warnings += [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}; tell the "
        "person, because removing it needs a manual database-history rewrite"
        for finding in outcome.secret_findings
    ]
    warnings += outcome.warnings
    # Guidance only, so broad on purpose: path resolution raises RuntimeError on a symlink
    # loop and git may be missing; none of it may cost the agent the confirmation of a write.
    try:
        warnings += uncaptured_location_warnings(
            entry.id,
            entry.current.version,
            entry.current.artifacts if artifacts is not None or creating else [],
            [body or "", json.dumps(value or {}, ensure_ascii=False)],
            [
                CapturedRepo(top=safe_resolve(Path(item.repo_path)), exclude=tuple(item.excluded))
                for item in summaries
            ],
        )
    except Exception as exc:
        warnings.append(f"written, but checking its paths failed: {exc}")
    embedded, embed_warnings = embed_after_write(store, [entry.id])
    warnings += embed_warnings
    graph = []
    if entry.kind in WORK_KINDS:
        graph, graph_warnings = redraw(store, [entry.id])
        warnings += graph_warnings
    return WriteResult(
        id=entry.id,
        kind=entry.kind,
        version=entry.current.version,
        created=outcome.created,
        retry=outcome.retry,
        projects=entry.projects,
        embedded=embedded,
        captures=summaries,
        graph=graph,
        warnings=warnings,
    )


def _landed(store: EntryStore, ref_name: str, code: list[CodeCapture]) -> bool:
    """Whether the version a capture ref `<id>/v<n>` was made for is durable with THIS
    capture, in which case its pin must stay. A version n written by a competing writer holds
    other captures, so its existence alone does not count."""
    if not code:
        return False
    entry_id, _, version = ref_name.partition("/v")
    rows = store.query(
        "SELECT captured_sha FROM version_code WHERE entry_id = ? AND version = ? AND "
        "captured_sha = ?",
        [entry_id, int(version), code[0].captured_sha],
    )
    return bool(rows)
