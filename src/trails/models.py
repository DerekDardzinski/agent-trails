"""The stored record, project, framing, and membership models.

These are the store's own types and the shapes every other unit consumes. A Record is what a
read produces; a RecordDraft is what a caller hands to `Store.write_record` after resolving any
human project names to stable project ids.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from trails.vocab import EntryType, ProjectAction, Relation, WorkStatus


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
    """What one publish captured from one repository."""

    repo_path: str
    git_common_dir: str
    captured_sha: str
    changed_files: int
    changed_bytes: int
    largest: list[tuple[str, int]]
    excluded: list[str]


class Link(BaseModel):
    """One outgoing relation from a record."""

    relation: Relation
    target_id: str


class WorkStatusEvent(BaseModel):
    """One append-only statement of whether more work is expected on a record."""

    sequence: int
    work_status: WorkStatus
    changed_at: datetime
    author: str
    reason: str | None


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


class ProjectMembershipEvent(BaseModel):
    """One append-only add or remove event for a record/project pair."""

    record_id: str
    project: ProjectRef
    sequence: int
    action: ProjectAction
    reason: str | None
    author: str
    created_at: datetime


class ProjectMembershipChange(BaseModel):
    """One effective membership change returned by set_projects."""

    project: ProjectRef
    action: ProjectAction
    reason: str | None
    author: str
    created_at: datetime


class Record(BaseModel):
    """A stored record with everything attached to it, as a read assembles it."""

    id: str
    entry_type: EntryType
    work_status: WorkStatus
    work_status_history: list[WorkStatusEvent]
    title: str
    body: str
    tags: list[str]
    projects: list[ProjectRef]
    author: str
    created_at: datetime
    value: dict[str, Any] | None
    reason: str | None
    artifacts: list[ArtifactRef]
    code: list[CodeCapture]
    links: list[Link]
    superseded_by: str | None
    retired: bool
    last_verified: tuple[date, str] | None


class RecordDraft(BaseModel):
    """What a caller asks the store to write; projects are resolved stable ids."""

    entry_type: EntryType
    work_status: WorkStatus
    id: str | None = None
    title: str
    body: str = ""
    tags: list[str] = []
    projects: list[str] | None = None
    value: dict[str, Any] | None = None
    links: list[Link] = []
    artifacts: list[ArtifactRef] = []
    code: list[CodeCapture] = []
    supersedes: str | None = None
    reason: str | None = None


class SecretFinding(BaseModel):
    """A possible secret, located without its text."""

    field: str
    line: int
    detector: str


class WriteOutcome(BaseModel):
    """A written record and possible secrets. A finding never stops a write."""

    record: Record
    secret_findings: list[SecretFinding]


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

    record_id: str
    changed: list[ProjectMembershipChange]
    unchanged_add: list[ProjectRef]
    unchanged_remove: list[ProjectRef]
    projects: list[ProjectRef]
