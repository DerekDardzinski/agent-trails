"""The store's own types: entries and their versions, links, task events, comments,
sessions, projects and framing. An Entry is what a read assembles; an EntryDraft is what a
caller hands to `EntryStore.write_entry`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from trails.vocab import (
    Kind,
    LinkAction,
    LinkRelation,
    Outcome,
    ProjectAction,
    StatedBy,
    TaskEventKind,
    TaskState,
    Verdict,
)


class ArtifactRef(BaseModel):
    """A pointer to data: a local path, an s3:// URI, query text, or free text."""

    location: str
    digest: str | None = None
    notes: str = ""


class CodeCapture(BaseModel):
    """One repository's working tree, captured as a pinned commit."""

    repo_path: str
    head_sha: str | None
    captured_sha: str
    dirty: bool


class CaptureSummary(BaseModel):
    """What one write captured from one repository."""

    repo_path: str
    git_common_dir: str
    captured_sha: str
    changed_files: int
    changed_bytes: int
    largest: list[tuple[str, int]]
    excluded: list[str]


class ProjectRef(BaseModel):
    """Stable project identity used wherever a record names its current projects."""

    id: str
    name: str


class ProjectFramingDraft(BaseModel):
    """Purpose and motivation supplied for a new project framing version."""

    title: str
    body: str = ""


class ProjectFramingVersion(BaseModel):
    """One append-only version of a project's purpose and motivation."""

    project_id: str
    version: int
    title: str
    body: str
    reason: str | None
    author: str
    created_at: datetime


class ProjectPath(BaseModel):
    """One place a project's code or data lives. trails records it and creates nothing there."""

    path: str = Field(
        description=(
            "A local path or an s3:// URI. A local path is stored after expanding ~, "
            "otherwise as given."
        )
    )
    label: str = Field(default="", description='Free text, for example "analysis code".')
    access_instructions: str = Field(
        default="",
        description=(
            'Free text saying how to reach it, for example "aws --profile '
            'rodimus-readonly-20260915".'
        ),
    )
    host: str = Field(
        default="",
        description="Machine a local path is on. Leave empty: it is filled for local paths.",
    )


class Project(BaseModel):
    """A stable body of work with current paths and optional current framing."""

    id: str
    name: str
    created_at: datetime
    created_by: str
    created_on_host: str
    paths: list[ProjectPath]
    current_framing: ProjectFramingVersion | None


class ProjectMembershipChange(BaseModel):
    """One effective membership change returned by set_projects."""

    project: ProjectRef
    action: ProjectAction
    reason: str | None
    author: str
    created_at: datetime


class SecretFinding(BaseModel):
    """A possible secret, located without its text."""

    field: str
    line: int
    detector: str


class ProjectWriteOutcome(BaseModel):
    """The atomic result of registering identity, paths, and optional framing."""

    project: Project
    created: bool
    changed: bool
    framing: ProjectFramingVersion | None
    framing_created: bool
    current_framing_version: int | None
    retry: bool


class SetProjectsOutcome(BaseModel):
    """The effective and idempotent parts of one membership request."""

    entry_id: str
    changed: list[ProjectMembershipChange]
    unchanged_add: list[ProjectRef]
    unchanged_remove: list[ProjectRef]
    projects: list[ProjectRef]


# ------------------------------------------------------------------ the entry model (redesign)


class Provenance(BaseModel):
    """Which harness and session wrote something, resolved without the agent's help.

    `harness` is always set ("unknown" when nothing identifies one). `session_id` is None when
    the harness gives no session; `transcript` and `agent` are None when they cannot be found.
    """

    harness: str
    session_id: str | None = None
    transcript: str | None = None
    agent: str | None = None


class EntryVersion(BaseModel):
    """One append-only version of an entry, carrying its full content."""

    entry_id: str
    version: int
    title: str
    body: str
    tags: list[str]
    value: dict[str, Any] | None
    priority: int | None
    stated_by: StatedBy | None
    artifacts: list[ArtifactRef]
    code: list[CodeCapture]
    reason: str | None
    author: str
    harness: str
    session_id: str | None
    created_at: datetime


class EntryLink(BaseModel):
    """One current outgoing link, with the target version it was written against."""

    source_id: str
    relation: LinkRelation
    target_id: str
    target_version: int | None
    reason: str | None
    author: str
    created_at: datetime


class LinkEvent(BaseModel):
    """One append-only add or remove of a link."""

    source_id: str
    relation: LinkRelation
    target_id: str
    sequence: int
    action: LinkAction
    target_version: int | None
    reason: str | None
    author: str
    harness: str
    session_id: str | None
    created_at: datetime


class TaskEvent(BaseModel):
    """One append-only claim, release, close or reopen of a goal or task."""

    entry_id: str
    sequence: int
    event: TaskEventKind
    outcome: Outcome | None
    reason: str | None
    author: str
    harness: str
    session_id: str | None
    created_at: datetime


class Entry(BaseModel):
    """An entry as a read assembles it: identity, current version and current state.

    `state`, `outcome` and `claim` are set for goals and tasks only. `claim` is the claim event
    currently held, if any.
    """

    id: str
    kind: Kind
    created_at: datetime
    current: EntryVersion
    projects: list[ProjectRef]
    links: list[EntryLink]
    state: TaskState | None
    outcome: Outcome | None
    claim: TaskEvent | None
    task_events: list[TaskEvent]
    retired: bool
    retired_reason: str | None


class EntryDraft(BaseModel):
    """What a caller asks the store to write.

    Without `id` it creates version 1, and `kind` and `title` are required. With `id` it
    writes the next version: `base_version` must equal the current version and `reason` must
    be nonempty. In a new version a field left None inherits the previous version's value;
    an explicit empty list clears it, and `value={}` clears the value (D14). `projects` None
    leaves membership unchanged, and a list replaces it.
    """

    kind: Kind | None = None
    id: str | None = None
    base_version: int | None = None
    reason: str | None = None
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    value: dict[str, Any] | None = None
    artifacts: list[ArtifactRef] | None = None
    code: list[CodeCapture] | None = None
    priority: int | None = None
    stated_by: StatedBy | None = None
    projects: list[str] | None = None
    parent: str | None = None
    waits_on: list[str] = []


class EntryWriteOutcome(BaseModel):
    """A written version, its warnings and possible secrets. `retry` is True when the call
    repeated a version that had already landed, so nothing new was written."""

    entry: Entry
    created: bool
    retry: bool
    warnings: list[str]
    secret_findings: list[SecretFinding]


class Comment(BaseModel):
    """A comment beside an entry. It creates no version."""

    id: str
    entry_id: str
    entry_version: int
    body: str
    verdict: Verdict | None
    addresses: list[str]
    addressed_by: list[str]
    author: str
    harness: str
    session_id: str | None
    created_at: datetime


class SessionRow(BaseModel):
    """One harness session, written the first time a write names it."""

    id: str
    harness: str
    transcript: str | None
    agent: str | None
    first_seen: datetime


class FollowUp(BaseModel):
    """A task to create when closing an entry; it is linked `discovered_from` the closed one."""

    title: str
    body: str = ""
    priority: int | None = None
    tags: list[str] = []


class LinkOutcome(BaseModel):
    """A link added or removed. `changed` is False when the link was already in that state."""

    source_id: str
    relation: LinkRelation
    target_id: str
    action: LinkAction
    changed: bool
    target_version: int | None
    warnings: list[str]


class MovedLink(BaseModel):
    """A waits_on link moved from a superseded entry onto its replacement."""

    source_id: str
    from_id: str
    to_id: str


class TaskOutcome(BaseModel):
    """A claim, release, close or reopen. `changed` is False when nothing was written."""

    entry: Entry
    event: TaskEvent | None
    changed: bool
    follow_ups: list[str]
    moved: list[MovedLink]
    warnings: list[str]


class RetireOutcome(BaseModel):
    """A retirement. `already_retired` is True when the entry was retired before; `reason`
    is then the reason that stands."""

    entry_id: str
    reason: str
    already_retired: bool


class CommentOutcome(BaseModel):
    """A written comment, the entry versions it pinned, and anything it closed."""

    comment: Comment
    pinned: dict[str, int]
    warnings: list[str]


class CommentPage(BaseModel):
    """The newest comments on one entry, newest first, with the total and open counts."""

    entry_id: str
    comments: list[Comment]
    total: int
    open: int
    offset: int


class VersionChange(BaseModel):
    """An entry whose version moved after a comment pinned it. `pinned` is None for an entry
    that was not under the commented entry when the comment was written."""

    entry_id: str
    title: str
    pinned: int | None
    current: int


class SinceComment(BaseModel):
    """What changed under a commented entry after the comment."""

    comment: Comment
    changed: list[VersionChange]
    unchanged: list[str]
    links: list[LinkEvent]
