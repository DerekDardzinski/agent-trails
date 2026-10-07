"""The record model: one stored shape with an explicit entry type and work status.

These are the store's own types and the shapes every other unit consumes. A Record is what a
read produces; a RecordDraft is what a caller hands to `Store.write_record`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from trails.vocab import EntryType, Relation, WorkStatus


class ArtifactRef(BaseModel):
    """A pointer to data: a local path, an s3:// URI, query text, or free text."""

    location: str
    digest: str | None = None  # "sha256:<64 hex>" when the agent recorded one, else None
    notes: str = ""  # what identifies this data: a version, a row count, a query date


class CodeCapture(BaseModel):
    """One repository's working tree, captured as a pinned commit."""

    repo_path: str  # absolute path of the git repository
    head_sha: str | None  # HEAD at capture time; None for a repository with no commit
    captured_sha: str  # commit of the full working tree, in trails' capture store
    dirty: bool  # True when the working tree differed from HEAD


class CaptureSummary(BaseModel):
    """What one publish captured from one repository, as the publish result reports it."""

    repo_path: str  # the repository's top level
    git_common_dir: str  # where its info/exclude file lives
    captured_sha: str
    changed_files: int  # files added or changed relative to HEAD; every file with no commit
    changed_bytes: int
    largest: list[tuple[str, int]]  # up to 3 (relpath, bytes) of the changed files, largest first
    excluded: list[str]  # the exclude entries applied, relative to repo_path


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


class Record(BaseModel):
    """A stored record with everything attached to it, as a read assembles it."""

    id: str
    entry_type: EntryType
    work_status: WorkStatus
    work_status_history: list[WorkStatusEvent]
    title: str
    body: str
    tags: list[str]  # descriptive tags, sorted, deduplicated, lowercase
    project: str | None
    author: str
    created_at: datetime  # UTC
    value: dict[str, Any] | None
    reason: str | None  # required when the record supersedes another
    artifacts: list[ArtifactRef]
    code: list[CodeCapture]
    links: list[Link]  # outgoing links written with the record or later
    superseded_by: str | None  # newest record linking `supersedes` onto this one
    retired: bool
    last_verified: tuple[date, str] | None  # (date, environment) from the newest stamp


class RecordDraft(BaseModel):
    """What a caller asks the store to write."""

    entry_type: EntryType
    work_status: WorkStatus
    id: str | None = None  # pre-minted id from Store.new_record_id(); None mints one
    title: str
    body: str = ""
    tags: list[str] = []
    project: str | None = None
    value: dict[str, Any] | None = None
    links: list[Link] = []
    artifacts: list[ArtifactRef] = []
    code: list[CodeCapture] = []
    supersedes: str | None = None
    reason: str | None = None


class SecretFinding(BaseModel):
    """A possible secret, located without its text."""

    field: str  # "title", "body", "value", "artifact[<n>].location", "file:<path>", ...
    line: int  # 1-based line within that field or file
    detector: str  # detect-secrets plugin name, e.g. "AWSKeyDetector"


class WriteOutcome(BaseModel):
    """A written record and the possible secrets found in it. A finding never stops a write."""

    record: Record
    secret_findings: list[SecretFinding]


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
    """A registered project: a name and the paths it uses. Nothing on disk."""

    name: str
    host: str  # machine the project was registered from
    created_at: datetime
    paths: list[ProjectPath]
